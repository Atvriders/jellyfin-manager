"""Static guards on the Jellyfield HD GLSL (no browser; runs in CI).

pow(x, y) is undefined for x < 0 in GLSL ES 3.00 (section 8.2): a driver that
implements it as exp2(y * log2(x)) returns NaN, which a float render target
keeps and the bloom then spreads over the frame. SwiftShader strength-reduces
a literal exponent of 2.0, so no e2e frame can catch it. Every pow() base in
the engine and in the species files must therefore be SYNTACTICALLY
non-negative: wrapped in max(), abs() or clamp(), or the engine's square helper
sq(). Squares are written sq(x) or x * x, never pow(x, 2.0).
"""
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "static"
FILES = ["jellyfield-hd.js", "jellyfield-species-striped.js"]   # the engine, then every species file
POW_CALL = re.compile(r"(?<![\w.])pow\s*\(")          # pow( and pow (; not Math.pow, not npow(
SAFE_WRAPPER = re.compile(r"^(max|abs|clamp|sq)\s*\(")


def _matching_paren(text, i):
    """Index of the ')' that closes the '(' at text[i], or -1."""
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return j
    return -1


def _base_is_safe(base):
    """The wrapper call must span the WHOLE base: `max(x, 0.0) - 1.0` and
    `abs(x) - 0.5` start with a wrapper and still go negative."""
    m = SAFE_WRAPPER.match(base)
    if not m:
        return False
    close = _matching_paren(base, m.end() - 1)
    return close != -1 and base[close + 1:].strip() == ""


def _strip_comments(text):
    """JS comments only: the GLSL lives in JS string literals and the WHY
    comments around them mention pow() by name. A block comment is replaced
    by its own newlines so the reported line numbers stay the file's."""
    text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def _pow_bases(text):
    """(line number, base expression) for every GLSL pow( call: the text between
    'pow(' and the first comma at parenthesis depth 0 on the same line.
    Math.pow (JS, where a negative base is fine) is excluded by the lookbehind."""
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        for m in POW_CALL.finditer(line):
            depth, i = 0, m.end()
            while i < len(line):
                ch = line[i]
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    if depth == 0:
                        break
                    depth -= 1
                elif ch == "," and depth == 0:
                    break
                i += 1
            out.append((n, line[m.end():i].strip()))
    return out


@pytest.mark.parametrize("name", FILES)
def test_every_pow_base_is_syntactically_non_negative(name):
    text = _strip_comments((STATIC / name).read_text())
    bad = [(n, b) for n, b in _pow_bases(text) if not _base_is_safe(b)]
    assert bad == [], f"{name}: pow() with an unguarded base (wrap it in max/abs/clamp, or use sq): {bad}"


def test_the_scanner_catches_a_negative_base():
    """The guard itself, checked once: a bare base fails, the wrapped forms and
    Math.pow pass, and a call inside a comment is ignored."""
    src = ("'  float a = exp(-pow((x - 0.3) / 0.1, 2.0));'\n"
           "'  float b = pow(max(x, 0.0), 3.0) + pow(abs(y), 2.0) + pow(clamp(z, 0.0, 1.0), 4.0) + pow(sq(w), 0.5);'\n"
           "var j = Math.pow(1 - u, 3);\n"
           "/* pow(prof, 8.0) is NaN */\n")
    bases = _pow_bases(_strip_comments(src))
    assert [b for _, b in bases if not _base_is_safe(b)] == ["(x - 0.3) / 0.1"]
    assert len(bases) == 5


def test_the_wrapper_must_span_the_whole_base():
    """A base that merely STARTS with a wrapper is not guarded: max(x, 0.0) - 1.0
    and abs(x) - 0.5 go negative. The wrapper's closing paren must be the end
    of the base (whitespace aside); nesting inside the wrapper is fine."""
    src = ("'  float a = pow(max(x, 0.0) - 1.0, 2.0);'\n"
           "'  float b = pow(abs(x) - 0.5, 3.0);'\n"
           "'  float c = pow(max(x, 0.0) * 2.0, 2.0);'\n"
           "'  float d = pow(max(sq(x) - 1.0, 0.0), 2.0) + pow(clamp(abs(x) - 0.5, 0.0, 1.0) , 3.0);'\n"
           "'  float e = pow(sq(x)/2.0, 2.0);'\n")
    bases = _pow_bases(_strip_comments(src))
    assert [b for _, b in bases if not _base_is_safe(b)] == [
        "max(x, 0.0) - 1.0", "abs(x) - 0.5", "max(x, 0.0) * 2.0", "sq(x)/2.0"]
    assert len(bases) == 6


def test_the_scanner_sees_pow_with_a_space_before_the_paren():
    """`pow (x, 2.0)` is the same GLSL call; the old `pow\\(` pattern missed it."""
    src = ("'  float a = pow (x, 2.0);'\n"
           "'  float b = pow  (max (x, 0.0), 2.0);'\n"
           "'  float c = npow(x, 2.0) + v.pow(x, 2.0);'\n")     # not GLSL pow: a different name, a JS method
    bases = _pow_bases(_strip_comments(src))
    assert [b for _, b in bases] == ["x", "max (x, 0.0)"]
    assert [b for _, b in bases if not _base_is_safe(b)] == ["x"]


@pytest.mark.parametrize("name", FILES[1:])
def test_species_defines_the_contract(name):
    text = (STATIC / name).read_text()
    for fn in ("vec3 bellShape(", "float bellThickness(", "vec4 bellSurface(", "vec4 organField(", "marginJS",
               "apexY:"):
        assert fn in text, f"{name} lacks {fn}"


# ---- THE ORGAN MARCH CONTRACT (SPECIES_HEAD in jellyfield-hd.js) ----
# A species may rely on exactly this: organField is called from ONE place, the
# near-wall march, in order, at p = organEntry + organDir * organStep * (k + 0.5).
# The striped species binds its stripes to organEntry and reads them on the first
# sample only, so a jittered start, another step order or a second caller
# would silently move or double them with every frame test still green.

def _glsl_lines(text):
    """The GLSL statements of a file: each JS string literal line's content,
    whitespace-normalised, comments stripped."""
    out = []
    for line in _strip_comments(text).splitlines():
        m = re.match(r"^\s*'(.*)',?\s*(\+.*)?$", line)
        if m:
            out.append(" ".join(m.group(1).split()))
    return out


MARCH = [
    "organEntry = v_lp;",
    "vec3 organs = vec3(0.0);",
    "float T = 1.0;",
    "if (u_wall > 0.5) {",
    "vec3 p = organEntry + organDir * (organStep * 0.5);",
    "for (int i = 0; i < 12; i++) {",
    "if (i >= u_organSteps) break;",
    "vec4 f = organField(p, u_time);",
    "organs += f.rgb * T * organStep;",
    "T *= exp(-f.a * organStep);",
    "p += organDir * organStep;",
    "}",
    "}",
]


def test_the_organ_march_is_the_documented_contract():
    """The near-wall march, statement for statement: organEntry is the wall's
    surface point, the first sample is half a step in from it, the samples go
    in order and organField's result is integrated as documented."""
    lines = _glsl_lines((STATIC / "jellyfield-hd.js").read_text())
    starts = [i for i, l in enumerate(lines) if l == MARCH[0]]
    assert len(starts) == 1, starts
    assert lines[starts[0]:starts[0] + len(MARCH)] == MARCH
    assert "vec3 organEntry = vec3(0.0);" in lines        # declared for the species (SPECIES_HEAD)


def test_organ_field_has_exactly_one_caller():
    """organField is called once in the engine (the march above) and never by a
    species: each species file holds only its definition."""
    call = re.compile(r"(?<![\w.])organField\s*\(")
    engine = _strip_comments((STATIC / "jellyfield-hd.js").read_text())
    assert [l.strip() for l in engine.splitlines() if call.search(l)] == ["'      vec4 f = organField(p, u_time);',"]
    for name in FILES[1:]:
        text = _strip_comments((STATIC / name).read_text())
        hits = [l.strip() for l in text.splitlines() if call.search(l)]
        assert hits == ["'vec4 organField(vec3 p, float time) {',"], (name, hits)


def test_engine_declares_sq_for_the_species():
    """sq() is part of what the species GLSL may rely on (SPECIES_HEAD)."""
    text = (STATIC / "jellyfield-hd.js").read_text()
    assert "float sq(float x) { return x * x; }" in text


def test_mote_period_is_assigned_before_its_first_use():
    """MOTE_T is a `var` in the engine's closure: read before its assignment it
    is undefined, and the silhouettes' breathing rates built from it were all
    NaN (sin(u_time * NaN) in SIL_VS). The one `var MOTE_T` line must come
    before every other line of code that names it."""
    lines = _strip_comments((STATIC / "jellyfield-hd.js").read_text()).splitlines()
    decl = [n for n, l in enumerate(lines) if re.search(r"\bvar MOTE_T\b", l)]
    uses = [n for n, l in enumerate(lines) if re.search(r"\bMOTE_T\b", l) and n not in decl]
    assert len(decl) == 1 and uses, (decl, uses)
    assert decl[0] < uses[0], f"MOTE_T read at line {uses[0] + 1}, assigned at line {decl[0] + 1}"


# ---- THE INEXTENSIBLE PASS (E-S, stepChains in jellyfield-hd.js) ----
# Three Gauss-Seidel passes let a long chain stretch with the frame time; the
# follow-the-leader pass after them caps every link at seg, and its velocity
# term (the parent gives back 0.9 of each correction) keeps the arms from
# climbing and coiling. Nothing in the frame tests would notice it gone.

def _js_lines(text):
    """The JS statements of a file, comments stripped, whitespace-normalised,
    blank lines dropped."""
    out = []
    for line in _strip_comments(text).splitlines():
        line = " ".join(line.split())
        if line:
            out.append(line)
    return out


GAUSS_SEIDEL = "for (it = 0; it < 3; it++) {"
FOLLOW_THE_LEADER = [
    "var seg2 = seg * seg;",
    "for (k = 1; k < len; k++) {",
    "i3 = (off + k) * 3; j3 = i3 - 3;",
    "var lx = ndPos[i3] - ndPos[j3];",
    "var ly = ndPos[i3 + 1] - ndPos[j3 + 1];",
    "var lz = ndPos[i3 + 2] - ndPos[j3 + 2];",
    "var l2 = lx * lx + ly * ly + lz * lz;",
    "if (l2 > seg2) {",
    "var ls = seg / Math.sqrt(l2) - 1;",
    "var cx = lx * ls, cy = ly * ls, cz = lz * ls;",
    "ndPos[i3] += cx; ndPos[i3 + 1] += cy; ndPos[i3 + 2] += cz;",
    "if (k > 1) {",
    "ndPrev[j3] += 0.9 * cx; ndPrev[j3 + 1] += 0.9 * cy; ndPrev[j3 + 2] += 0.9 * cz;",
    "}",
    "}",
    "}",
]


def test_the_inextensible_pass_follows_the_gauss_seidel_iterations():
    """The follow-the-leader pass, statement for statement, starting on the
    line right after the Gauss-Seidel loop closes: it only shortens (l2 >
    seg2), walks from the pinned root outward, and takes 0.9 of each node's
    correction back out of its parent's velocity (never the root's)."""
    lines = _js_lines((STATIC / "jellyfield-hd.js").read_text())
    gs = [i for i, l in enumerate(lines) if l == GAUSS_SEIDEL]
    assert len(gs) == 1, gs
    depth, end = 0, -1
    for i in range(gs[0], len(lines)):
        depth += lines[i].count("{") - lines[i].count("}")
        if depth == 0:
            end = i
            break
    assert end > gs[0]
    assert lines[end + 1:end + 1 + len(FOLLOW_THE_LEADER)] == FOLLOW_THE_LEADER
    assert sum(l == FOLLOW_THE_LEADER[0] for l in lines) == 1
