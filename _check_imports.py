"""§5 requirements check — standalone script."""
import ast
import pathlib
import sys

stdlib = set(sys.stdlib_module_names)
root = pathlib.Path(".")
local = {p.stem for p in root.glob("*.py")}
found = set()
for p in root.rglob("*.py"):
    parts = str(p)
    if ".venv" in parts or "__pycache__" in parts or ".claude" in parts:
        continue
    try:
        tree = ast.parse(p.read_text(encoding="utf-8"))
    except SyntaxError:
        continue
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                found.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                found.add(node.module.split(".")[0])

reqs = set()
for line in open("requirements.txt"):
    line = line.strip()
    if line and not line.startswith("#"):
        for sep in ("==", ">=", "<=", "~=", ">", "<"):
            line = line.split(sep)[0]
        reqs.add(line.split("[")[0].strip().lower().replace("-", "_"))

ALIAS = {
    "argon2": "argon2_cffi", "dotenv": "python_dotenv",
    "jwt": "pyjwt", "yaml": "pyyaml", "PIL": "pillow",
    "multipart": "python_multipart",
}
TEST_ONLY = {
    "pytest", "playwright", "selenium", "quickjs",
    "httpx", "requests", "pyflakes", "fnmatch",
}
missing = sorted(
    m for m in found
    if m not in stdlib and m not in local
    and m not in TEST_ONLY
    and m.lower().replace("-", "_") not in reqs
    and ALIAS.get(m, "").lower() not in reqs
)
print("MISSING from requirements.txt:", missing or "none")
