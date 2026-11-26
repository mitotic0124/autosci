"""Reject TeX damaged by JSON escapes before it can look like valid mathematics."""
import re


def escape_currency_dollars(text: str) -> str:
    # Quoted market questions often contain several dollar amounts in one line.
    # Their currency signs must not pair up as a single mathematical expression.
    return re.sub(
        r"(?<![\\$])\$(\d+(?:,\d{3})*(?:\.\d+)?[kKmMbB]?\+?)"
        r"(?=\s+(?:by|per|million|billion|thousand|in|at|on|and|for|of)\b|[，。！？!?;\"]|$)",
        lambda match: r"\$" + match.group(1), text,
    )


def malformed_math_reason(text: str, *, formula_only: bool = False) -> str | None:
    expressions = [text] if formula_only else [
        match.group(1) or match.group(2) or match.group(3)
        for match in re.finditer(
            r"(?<![\\$])\${1,2}([^$]+)(?<!\\)\${1,2}"
            r"|\\\((.*?)\\\)|\\\[([\s\S]*?)\\\]", text,
        )
    ]
    for expression in expressions:
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", expression):
            return "LaTeX contains JSON control characters"
        broken = re.search(
            r"(?<![\\A-Za-z])(?:ext(?:sc|bf|it|rm|sf)?|(?:t|d)?frac|rac|egin|"
            r"operatorname|mathrm|mathbf|mathbb|mathcal|sqrt)(?=\s*(?:\{|\d))",
            expression,
        )
        if broken:
            return f"LaTeX command lost its backslash: {broken.group()}"
    return None
