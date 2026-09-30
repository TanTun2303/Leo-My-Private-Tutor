<!-- case: inline-dollar | expect: math -->
Inline with dollars: $a^2 + b^2 = c^2$ is Pythagoras.

<!-- case: inline-paren | expect: math -->
Inline with parens: \(\frac{n(n+1)}{2}\) is the triangular number.

<!-- case: inline-double | expect: math -->
Inline with double dollars: $$\sqrt{2}$$ is irrational.

<!-- case: display-dollar-one-line | expect: math -->
$$\sum_{k=0}^{n} r^k = \frac{1 - r^{n+1}}{1 - r}$$

<!-- case: display-dollar-multiline | expect: math -->
The recurrence is
$$
T(n) = 2T\left(\frac{n}{2}\right) + \Theta(n)
$$
which solves to $\Theta(n \log n)$.

<!-- case: display-dollar-glued | expect: math -->
So we get $$\int_0^1 x^2\,dx = \frac{1}{3}$$ as claimed.

<!-- case: display-bracket-one-line | expect: math -->
\[ \lim_{x \to 0} \frac{\sin x}{x} = 1 \]

<!-- case: display-bracket-multiline | expect: math -->
Euler's identity:
\[
e^{i\pi} + 1 = 0
\]

<!-- case: aligned | expect: math -->
$$
\begin{aligned}
(a+b)^2 &= a^2 + 2ab + b^2 \\
        &\ge 4ab
\end{aligned}
$$

<!-- case: align-env | expect: math -->
\begin{align}
f(n) &= f(n-1) + f(n-2) \label{eq:fib} \\
f(0) &= 0
\end{align}

<!-- case: cases | expect: math -->
$$
|x| = \begin{cases} x & \text{if } x \ge 0 \\ -x & \text{otherwise} \end{cases}
$$

<!-- case: pmatrix | expect: math -->
The rotation matrix is $$R = \begin{pmatrix} \cos\theta & -\sin\theta \\ \sin\theta & \cos\theta \end{pmatrix}$$

<!-- case: boxed | expect: math -->
Therefore the answer is $\boxed{42}$.

<!-- case: tag | expect: math -->
$$
E = mc^2 \tag{1}
$$

<!-- case: big-operators | expect: math -->
We use $\sum_{i=1}^{n} i$, $\int_a^b f(x)\,dx$ and $\lim_{n\to\infty} a_n$ together.

<!-- case: operatorname-text | expect: math -->
Define $\operatorname{lcm}(a, b) = \frac{|ab|}{\gcd(a,b)}$ when $\text{both are nonzero}$.

<!-- case: korean-inline | expect: math -->
함수 $f(x)=x^2$의 도함수는 $f'(x)=2x$이다.

<!-- case: korean-paren | expect: math -->
따라서 \(O(n \log n)\)의 시간이 걸린다.

<!-- case: list-marker | expect: math -->
- $a_n \to 0$ is necessary.
1. \(\sum a_n\) converges absolutely.

<!-- case: table-cell | expect: math -->
| Algorithm | Time |
|---|---|
| Merge sort | $O(n \log n)$ |
| Dijkstra (heap) | \(O((V+E)\log V)\) |

<!-- case: currency | expect: literal -->
The book costs $5 and the course costs $10.

<!-- case: code-block | expect: code -->
```python
price = "$x$"  # must stay literal
```
