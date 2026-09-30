"""Book ingestion for Leo (§8): Marker → per-chapter Markdown → Open WebUI Knowledge.

Commands (run by `make ingest`, `make library`, `make library-remove BOOK=`):
    python ingest.py run        convert + upload every new file in /library/inbox
    python ingest.py list       show ingested books
    python ingest.py remove SLUG
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

LIB = Path(os.environ.get("LIBRARY_DIR", "/library"))
INBOX, MARKDOWN, PROCESSED = LIB / "inbox", LIB / "markdown", LIB / "processed"
MANIFEST = LIB / "manifest.json"
WEBUI = os.environ.get("WEBUI_INTERNAL_URL", "http://open-webui:8080").rstrip("/")
NORMALIZER = Path(os.environ.get("NORMALIZER_PATH", "/normalizer/leo_latex_normalizer.py"))
SUPPORTED = {".pdf", ".epub", ".docx"}
SKIP_OUTLINE = re.compile(r"^(contents|table of contents|index|cover|title page|copyright)$", re.I)


def env(key: str, default: str = "") -> str:
    return re.sub(r"\s+#.*$", "", os.environ.get(key, default)).strip()


def slugify(text: str, max_len: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:max_len].rstrip("-") or "untitled"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def load_manifest() -> dict[str, Any]:
    try:
        data = json.loads(MANIFEST.read_text())
        return data if isinstance(data, dict) and "books" in data else {"books": {}}
    except (FileNotFoundError, json.JSONDecodeError):
        return {"books": {}}


def save_manifest(m: dict[str, Any]) -> None:
    tmp = MANIFEST.with_suffix(".tmp")
    tmp.write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(MANIFEST)


def load_normalize():
    spec = importlib.util.spec_from_file_location("leo_latex_normalizer", NORMALIZER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.normalize_math


# ── metadata & chapters ─────────────────────────────────────────────────────


def title_author(path: Path) -> tuple[str, str]:
    """'Author - Title (year, publisher).pdf' → (title, author); falls back to PDF metadata / filename."""
    stem = re.sub(r"\s*\([^)]*\)\s*$", "", path.stem).strip()
    if " - " in stem:
        author, title = stem.split(" - ", 1)
        return title.strip(), author.strip()
    if path.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader

            meta = PdfReader(str(path)).metadata or {}
            if meta.get("/Title"):
                return str(meta["/Title"]), str(meta.get("/Author") or "Unknown")
        except Exception:  # noqa: BLE001 — metadata is optional
            pass
    return stem, "Unknown"


@dataclass
class Chapter:
    number: int
    title: str
    first_page: int  # 0-based, inclusive
    last_page: int  # 0-based, inclusive


def pdf_chapters(path: Path) -> list[Chapter]:
    """Top-level outline entries → page ranges. Falls back to one chapter for the whole book."""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    n = len(reader.pages)
    entries: list[tuple[str, int]] = []
    try:
        for item in reader.outline:
            if isinstance(item, list):
                continue
            try:
                page = reader.get_destination_page_number(item)
            except Exception:  # noqa: BLE001
                continue
            title = " ".join(str(item.title).split())
            if page is not None and page >= 0:
                entries.append((title, page))
    except Exception:  # noqa: BLE001 — no/broken outline
        entries = []
    entries.sort(key=lambda e: e[1])
    # Drop a leading entry that is just the book title / title page.
    if len(entries) > 1 and (entries[0][1] == entries[1][1] or entries[1][1] - entries[0][1] <= 2):
        entries = entries[1:]
    chapters: list[Chapter] = []
    for i, (title, start) in enumerate(entries):
        end = (entries[i + 1][1] - 1) if i + 1 < len(entries) else n - 1
        if end < start or SKIP_OUTLINE.match(title):
            continue
        chapters.append(Chapter(len(chapters) + 1, title, start, end))
    return chapters or [Chapter(1, path.stem, 0, n - 1)]


# ── conversion ──────────────────────────────────────────────────────────────


class Converter:
    """Thin wrapper around Marker 2.0; model clients are created once per run."""

    def __init__(self, mode: str) -> None:
        from marker.models import create_model_dict

        self.mode = mode if mode in ("balanced", "fast") else "balanced"
        self.models = create_model_dict()

    def convert(self, path: Path, page_range: str | None) -> str:
        from marker.config.parser import ConfigParser
        from marker.converters.pdf import PdfConverter
        from marker.output import text_from_rendered

        cfg: dict[str, Any] = {"output_format": "markdown", "mode": self.mode, "disable_image_extraction": True}
        if page_range:
            cfg["page_range"] = page_range
        parser = ConfigParser(cfg)
        converter = PdfConverter(
            config=parser.generate_config_dict(),
            artifact_dict=self.models,
            processor_list=parser.get_processors(),
            renderer=parser.get_renderer(),
        )
        text, _, _ = text_from_rendered(converter(str(path)))
        return text


_IMG = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_SPAN_ID = re.compile(r'<span id="[^"]*"></span>')
_MATH_HEADING = re.compile(r"^(?:- )?#{1,6}\s+\$([^$]+)\$\s*$", re.M)  # Marker quirk: "## $u+v=2$"


def postprocess(md: str, normalize) -> str:
    md = _IMG.sub(lambda m: f"*Figure: {m.group(1).strip()}*" if m.group(1).strip() else "", md)
    md = _SPAN_ID.sub("", md)
    md = _MATH_HEADING.sub(lambda m: f"\n$$\n{m.group(1).strip()}\n$$\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md).strip() + "\n"
    return normalize(md, env("LEO_MATH_INLINE", "dollar"))


def front_matter(**fields: Any) -> str:
    lines = ["---"]
    for k, v in fields.items():
        lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
    return "\n".join(lines) + "\n---\n\n"


_WORKER: Converter | None = None


def _convert_chapter(
    args: tuple[str, int, str, int, int, str, str, str, str],
) -> tuple[int, str, str, int | None, int, float]:
    """Process-pool worker: convert one chapter to a Markdown file. Returns (number, file, title, pages, chars, secs)."""
    global _WORKER
    path_s, number, ch_title, first, last, slug, title, author, digest = args
    t = time.time()
    path = Path(path_s)
    pr = f"{first}-{last}" if first >= 0 else None
    name = f"{slug}__ch{number:02d}-{slugify(ch_title, 40)}.md"
    out = MARKDOWN / name
    # Reuse a chapter converted by an interrupted run (same source file hash in its front matter).
    if out.exists() and f'sha256: "{digest}"' in out.read_text()[:2000]:
        body = out.read_text().split("\n---\n\n", 1)[-1]
        return number, name, ch_title, (last - first + 1) if pr else None, len(body), 0.0
    if _WORKER is None:
        _WORKER = Converter(env("INGEST_MARKER_MODE", "balanced"))
    md = postprocess(_WORKER.convert(path, pr), load_normalize())
    out.write_text(
        front_matter(title=title, author=author, source_file=path.name, chapter=ch_title, sha256=digest) + md
    )
    return number, name, ch_title, (last - first + 1) if pr else None, len(md), time.time() - t


# ── Open WebUI ──────────────────────────────────────────────────────────────


class WebUI:
    def __init__(self) -> None:
        self.http = httpx.Client(base_url=WEBUI, timeout=300)
        r = self.http.post(
            "/api/v1/auths/signin",
            json={"email": env("WEBUI_ADMIN_EMAIL"), "password": env("WEBUI_ADMIN_PASSWORD")},
        )
        r.raise_for_status()
        self.http.headers["Authorization"] = f"Bearer {r.json()['token']}"

    def knowledge_id(self, name: str) -> str:
        page = 1
        while True:
            res = self.http.get("/api/v1/knowledge/", params={"page": page}).json()
            for kb in res.get("items", []):
                if kb.get("name") == name:
                    return kb["id"]
            if not res.get("items") or page * 30 >= res.get("total", 0):
                break
            page += 1
        sys.exit(f"Knowledge base '{name}' not found — run `make bootstrap` first")

    def upload(self, path: Path, kb_id: str) -> str:
        with path.open("rb") as f:
            r = self.http.post("/api/v1/files/", files={"file": (path.name, f, "text/markdown")})
        r.raise_for_status()
        fid = r.json()["id"]
        deadline = time.time() + 900
        while time.time() < deadline:
            st = self.http.get(f"/api/v1/files/{fid}/process/status").json().get("status")
            if st == "completed":
                break
            if st == "failed":
                raise RuntimeError(f"Open WebUI failed to process {path.name}")
            time.sleep(2)
        r = self.http.post(f"/api/v1/knowledge/{kb_id}/file/add", json={"file_id": fid})
        if r.status_code >= 400 and "Duplicate" not in r.text:
            raise RuntimeError(f"adding {path.name} to knowledge failed: {r.status_code} {r.text[:200]}")
        return fid

    def remove(self, kb_id: str, fid: str) -> None:
        self.http.post(f"/api/v1/knowledge/{kb_id}/file/remove", json={"file_id": fid})
        self.http.delete(f"/api/v1/files/{fid}")


# ── commands ────────────────────────────────────────────────────────────────


def cmd_run() -> int:
    for d in (INBOX, MARKDOWN, PROCESSED):
        d.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest()
    files = sorted(p for p in INBOX.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED)
    todo = []
    for p in files:
        digest = sha256(p)
        if digest in manifest["books"]:
            print(f"= {p.name}: already ingested as '{manifest['books'][digest]['slug']}' — moving to processed/")
            shutil.move(str(p), PROCESSED / p.name)
            continue
        todo.append((p, digest))
    if not todo:
        print("Nothing new in library/inbox.")
        return 0

    ui = WebUI()
    kb_id = ui.knowledge_id(env("LEO_KNOWLEDGE_NAME", "Leo Library"))
    workers = max(1, int(env("INGEST_WORKERS", "3") or 3))
    mode = env("INGEST_MARKER_MODE", "balanced")
    summary = []
    for path, digest in todo:
        title, author = title_author(path)
        slug = slugify(title)
        print(
            f"\n==> {path.name}\n    title: {title} · author: {author} · slug: {slug} · {workers} workers", flush=True
        )
        chapters = pdf_chapters(path) if path.suffix.lower() == ".pdf" else [Chapter(1, title, -1, -1)]
        jobs = [
            (str(path), c.number, c.title, c.first_page, c.last_page, slug, title, author, digest) for c in chapters
        ]
        # Resume: chapters finished by an earlier, interrupted run are kept (manifest "partial").
        records: list[dict[str, Any]] = [
            r for r in manifest.setdefault("partial", {}).get(digest, []) if (MARKDOWN / r["file"]).exists()
        ]
        done = {r["number"] for r in records}
        if done:
            print(f"    resuming: {len(done)} chapter(s) already uploaded", flush=True)
        jobs = [j for j in jobs if j[1] not in done]
        t0 = time.time()
        try:
            with ProcessPoolExecutor(max(1, min(workers, len(jobs)))) as pool:
                # Largest chapters first keeps the workers busy; uploads happen as results arrive.
                futures = [pool.submit(_convert_chapter, j) for j in sorted(jobs, key=lambda j: j[3] - j[4])]
                for fut in as_completed(futures):
                    number, name, ch_title, pages, chars, secs = fut.result()
                    fid = ui.upload(MARKDOWN / name, kb_id)
                    records.append(
                        {
                            "number": number,
                            "file": name,
                            "file_id": fid,
                            "chapter": ch_title,
                            "pages": pages,
                            "chars": chars,
                        }
                    )
                    manifest["partial"][digest] = records
                    save_manifest(manifest)
                    print(
                        f"    ✔ ch{number:02d} {ch_title[:48]:<48} {pages or '-':>4} pages  {chars:>7} chars  "
                        f"{secs:5.0f}s",
                        flush=True,
                    )
        except Exception as e:
            print(
                f"    ✘ {path.name} failed ({type(e).__name__}: {e}); finished chapters are kept — "
                "rerun `make ingest` to resume",
                flush=True,
            )
            raise
        manifest["partial"].pop(digest, None)
        records.sort(key=lambda r: r["number"])
        manifest["books"][digest] = {
            "slug": slug,
            "title": title,
            "author": author,
            "source_file": path.name,
            "ingested_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "mode": mode,
            "chapters": records,
        }
        save_manifest(manifest)
        shutil.move(str(path), PROCESSED / path.name)
        summary.append((slug, len(records), sum(r["pages"] or 0 for r in records), time.time() - t0))

    print("\n==> Summary")
    print(f"  {'book':<50} {'chapters':>8} {'pages':>6} {'time':>7}")
    for slug, nch, pages, secs in summary:
        print(f"  {slug:<50} {nch:>8} {pages:>6} {secs / 60:6.1f}m")
    return 0


def cmd_list() -> int:
    books = load_manifest()["books"]
    if not books:
        print("Library is empty.")
        return 0
    print(f"  {'slug':<50} {'chapters':>8}  {'ingested':<25} title")
    for b in books.values():
        print(f"  {b['slug']:<50} {len(b['chapters']):>8}  {b['ingested_at']:<25} {b['title']} — {b['author']}")
    return 0


def cmd_remove(slug: str) -> int:
    manifest = load_manifest()
    hit = [(d, b) for d, b in manifest["books"].items() if b["slug"] == slug]
    if not hit:
        sys.exit(f"No book with slug '{slug}'. Try `make library`.")
    ui = WebUI()
    kb_id = ui.knowledge_id(env("LEO_KNOWLEDGE_NAME", "Leo Library"))
    for digest, book in hit:
        for ch in book["chapters"]:
            ui.remove(kb_id, ch["file_id"])
            (MARKDOWN / ch["file"]).unlink(missing_ok=True)
        del manifest["books"][digest]
    save_manifest(manifest)
    print(f"Removed '{slug}' from the knowledge base (original stays in library/processed/).")
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        return cmd_run()
    if cmd == "list":
        return cmd_list()
    if cmd == "remove" and len(sys.argv) > 2:
        return cmd_remove(sys.argv[2])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
