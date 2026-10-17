"""Benchmark tasks: each has project files, a request, an optional test command, and a hidden check (not visible to the agents)."""
import subprocess, sys, shutil
from pathlib import Path

PY = sys.executable


def run(cmd, cwd, inp=None):
    p = subprocess.run(cmd, cwd=cwd, input=inp, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, shell=isinstance(cmd, str))
    return p.returncode == 0, (p.stdout + p.stderr)[-1500:]


def pycheck(code):
    return lambda ws: run([PY, "-I", "-c", "import sys; sys.path.insert(0, '.')\n" + code], ws)


INVENTORY = "\n\n".join(
    [f'def item_{i}(stock):\n    """Helper {i}."""\n    return stock.get("item_{i}", 0) + {i}\n' for i in range(40)]
    + ['MAX_STOCK = 500\n\n\ndef restock(stock, name, qty):\n    """Add qty units of name to stock."""\n'
       '    stock[name] = stock.get(name, 0) + qty\n    return stock[name]\n']
    + [f'def report_{i}(stock):\n    """Report {i}."""\n    return sorted(stock)[:{i + 1}]\n' for i in range(40)])

TASKS = [
    dict(id="py_bug_th", files={
        "stats.py": "def median(xs):\n    s = sorted(xs)\n    return s[len(s) // 2]\n",
        "main.py": "from stats import median\n\nprint(median([3, 1, 2]))\n"},
        task="แก้บั๊ก median ใน stats.py ให้ถูกต้องเมื่อจำนวนข้อมูลเป็นเลขคู่ (ต้องคืนค่าเฉลี่ยของสองตัวกลาง)",
        check=pycheck("from stats import median\nassert median([1,2,3,4]) == 2.5\nassert median([3,1,2]) == 2\nassert median([5]) == 5")),

    dict(id="py_multireq", files={
        "validators.py": "def parse_age(s):\n    return int(s)\n",
        "tests/__init__.py": "",
        "tests/test_validators.py": "import unittest\nfrom validators import parse_age\n\n\nclass T(unittest.TestCase):\n"
                                    "    def test_basic(self):\n        self.assertEqual(parse_age('30'), 30)\n"},
        test_cmd="python -m unittest discover -q -s tests -t .",
        task="parse_age ใน validators.py ต้อง (1) ตัดช่องว่างหัวท้ายก่อนแปลง (2) raise ValueError ถ้าอายุน้อยกว่า 0 หรือมากกว่า 150 "
             "(3) raise ValueError พร้อมข้อความ 'not a number' ถ้าไม่ใช่ตัวเลข และเพิ่มเทสต์ทั้ง 3 กรณีใน tests/test_validators.py",
        check=lambda ws: _all(
            pycheck("from validators import parse_age as p\nassert p(' 42 ') == 42\n"
                    "for bad in ['-1','151']:\n  try: p(bad)\n  except ValueError: pass\n  else: raise SystemExit('no error for '+bad)\n"
                    "try: p('abc')\nexcept ValueError as e: assert 'not a number' in str(e), e\nelse: raise SystemExit('abc')")(ws),
            _min_tests(ws / "tests/test_validators.py", 4),
            run([PY, "-m", "unittest", "discover", "-q", "-s", "tests", "-t", "."], ws))),

    dict(id="py_rename", files={
        "billing.py": "def calc_total(items):\n    return sum(p * q for p, q in items)\n",
        "app.py": "from billing import calc_total\n\n\ndef checkout(cart):\n    return round(calc_total(cart) * 1.07, 2)\n",
        "tests/__init__.py": "",
        "tests/test_billing.py": "import unittest\nfrom billing import calc_total\n\n\nclass T(unittest.TestCase):\n"
                                 "    def test_total(self):\n        self.assertEqual(calc_total([(2, 3)]), 6)\n"},
        test_cmd="python -m unittest discover -q -s tests -t .",
        task="Rename the function calc_total to compute_total everywhere in the project (definition, callers and tests).",
        check=lambda ws: _all(_absent(ws, "calc_total"),
                              pycheck("from app import checkout\nfrom billing import compute_total\nassert checkout([(10, 1)]) == 10.7")(ws),
                              run([PY, "-m", "unittest", "discover", "-q", "-s", "tests", "-t", "."], ws))),

    dict(id="py_wrong_file", files={
        "config/__init__.py": "", "config/dev.py": "TIMEOUT = 30\nDEBUG = True\n", "config/prod.py": "TIMEOUT = 30\nDEBUG = False\n",
        "server.py": "from config import prod\n\nprint(prod.TIMEOUT)\n"},
        task="เปลี่ยน TIMEOUT ของ production เป็น 60 วินาที (dev ต้องคงเดิม)",
        check=lambda ws: _all(pycheck("from config import dev, prod\nassert prod.TIMEOUT == 60, prod.TIMEOUT\nassert dev.TIMEOUT == 30\nassert prod.DEBUG is False")(ws),
                              _same(ws, "config/dev.py"), _same(ws, "server.py"))),

    dict(id="py_bigfile", files={"inventory.py": INVENTORY},
        task="In inventory.py, change restock so the stored amount never exceeds MAX_STOCK (cap it) and it raises ValueError when qty is negative.",
        check=lambda ws: _all(
            pycheck("from inventory import restock, item_39, report_0\ns={'a':490}\nassert restock(s,'a',20)==500 and s['a']==500\n"
                    "assert restock({}, 'b', 3) == 3\ntry: restock({}, 'c', -1)\nexcept ValueError: pass\nelse: raise SystemExit('neg')\n"
                    "assert item_39({}) == 39 and report_0({'x':1}) == ['x']")(ws),
            _untouched_lines(ws / "inventory.py", INVENTORY, "def restock"))),

    dict(id="py_crossfile", files={
        "shop/__init__.py": "",
        "shop/discount.py": "def flat_off(total, amount):\n    return max(0, total - amount)\n",
        "shop/cart.py": "class Cart:\n    def __init__(self):\n        self.items = []\n\n    def add(self, price, qty=1):\n"
                        "        self.items.append((price, qty))\n\n    def total(self):\n        return sum(p * q for p, q in self.items)\n"},
        task="เพิ่มส่วนลดแบบเปอร์เซ็นต์: สร้างฟังก์ชัน percent_off(total, pct) ใน shop/discount.py (pct ต้องอยู่ระหว่าง 0-100 ไม่งั้น ValueError) "
             "แล้วให้ Cart.total รับพารามิเตอร์ coupon_pct=0 และใช้ percent_off คำนวณ",
        check=pycheck("from shop.cart import Cart\nfrom shop.discount import percent_off, flat_off\nc=Cart(); c.add(100,2)\n"
                      "assert c.total()==200 and abs(c.total(coupon_pct=25)-150)<1e-9\nassert percent_off(80, 50)==40\n"
                      "try: percent_off(10, 101)\nexcept ValueError: pass\nelse: raise SystemExit('pct')\nassert flat_off(5, 10)==0\n"
                      "import inspect, shop.cart\nassert 'percent_off' in inspect.getsource(shop.cart)")),

    dict(id="py_nofile_named", files={
        "users.py": "import json\n\n\ndef load_users(path):\n    with open(path, encoding='utf-8') as f:\n        return json.load(f)\n",
        "data.py": "def normalize(users):\n    return [u.strip().lower() for u in users]\n",
        "main.py": "from users import load_users\nfrom data import normalize\n\nprint(normalize(load_users('users.json')))\n"},
        task="ฟังก์ชัน load_users ต้องคืนลิสต์ว่างแทนการ error เมื่อไม่มีไฟล์อยู่",
        check=lambda ws: _all(pycheck("from users import load_users\nassert load_users('nope_missing.json') == []\n"
                                      "import json,tempfile,os\nf=tempfile.mktemp(); open(f,'w').write('[\"A\"]')\nassert load_users(f)==['A']")(ws),
                              _same(ws, "data.py"), _same(ws, "main.py"))),

    dict(id="js_bug", files={
        "package.json": '{"name":"s","version":"1.0.0","scripts":{"test":"node --test"}}\n',
        "src/slugify.js": "function slugify(s) {\n  return s.trim().replace(/ /g, '-');\n}\n\nmodule.exports = { slugify };\n",
        "test/slugify.test.js": "const test = require('node:test');\nconst assert = require('node:assert');\nconst { slugify } = require('../src/slugify');\n\n"
                                "test('spaces', () => assert.strictEqual(slugify('a b'), 'a-b'));\n"},
        test_cmd="npm test --silent",
        task="Fix slugify in src/slugify.js: output must be lowercase, runs of spaces/underscores become a single '-', and other non-alphanumeric characters are removed.",
        check=lambda ws: run(["node", "-e", "const {slugify}=require('./src/slugify');const a=require('assert');"
                              "a.strictEqual(slugify(' Hello  World_Foo! '),'hello-world-foo');a.strictEqual(slugify('a b'),'a-b');"], ws)),

    dict(id="js_feature_test", files={
        "package.json": '{"name":"q","version":"1.0.0","scripts":{"test":"node --test"}}\n',
        "src/queue.js": "class Queue {\n  constructor() { this.items = []; }\n  push(x) { this.items.push(x); }\n"
                        "  pop() { return this.items.shift(); }\n  get size() { return this.items.length; }\n}\n\nmodule.exports = { Queue };\n",
        "test/queue.test.js": "const test = require('node:test');\nconst assert = require('node:assert');\nconst { Queue } = require('../src/queue');\n\n"
                              "test('fifo', () => { const q = new Queue(); q.push(1); q.push(2); assert.strictEqual(q.pop(), 1); });\n"},
        test_cmd="npm test --silent",
        task="เพิ่มเมธอด peek() ใน Queue (src/queue.js) ที่คืนตัวแรกโดยไม่เอาออก และคืน undefined ถ้าคิวว่าง พร้อมเพิ่มเทสต์ใน test/queue.test.js",
        check=lambda ws: _all(run(["node", "-e", "const {Queue}=require('./src/queue');const a=require('assert');const q=new Queue();"
                                   "a.strictEqual(q.peek(),undefined);q.push(7);q.push(8);a.strictEqual(q.peek(),7);a.strictEqual(q.size,2);"], ws),
                              _contains(ws / "test/queue.test.js", "peek"), run("npm test --silent", ws))),

    dict(id="rust_bug", files={
        "Cargo.toml": '[package]\nname = "fb"\nversion = "0.1.0"\nedition = "2021"\n',
        "src/lib.rs": "pub fn fizzbuzz(n: u32) -> String {\n    if n % 3 == 0 {\n        \"Fizz\".to_string()\n    } else if n % 5 == 0 {\n"
                      "        \"Buzz\".to_string()\n    } else {\n        n.to_string()\n    }\n}\n\n#[cfg(test)]\nmod tests {\n    use super::*;\n\n"
                      "    #[test]\n    fn three() {\n        assert_eq!(fizzbuzz(3), \"Fizz\");\n    }\n}\n"},
        test_cmd="cargo test -q",
        task="fizzbuzz in src/lib.rs returns \"Fizz\" for 15 but must return \"FizzBuzz\" for multiples of both 3 and 5. Fix it and add a test for 15.",
        check=lambda ws: _all(_contains(ws / "src/lib.rs", "15"), _rust_hidden(ws))),
]


def _all(*results):
    bad = [m for ok, m in results if not ok]
    return (not bad), " | ".join(bad)[:1500]


def _same(ws, rel):
    return (ws / rel).read_bytes() == (ws.parent / "orig" / rel).read_bytes(), f"{rel} ถูกแก้ทั้งที่ไม่ควร"


def _contains(p, s):
    return p.exists() and s in p.read_text(encoding="utf-8"), f"{p.name} ไม่มี {s!r}"


def _absent(ws, word):
    hits = [str(p.relative_to(ws)) for p in ws.rglob("*.py") if ".office_bak" not in p.parts and word in p.read_text(encoding="utf-8")]
    return not hits, f"ยังเหลือ {word} ใน {hits}"


def _min_tests(p, n):
    c = p.read_text(encoding="utf-8").count("def test") if p.exists() else 0
    return c >= n, f"เทสต์มี {c} ต้องอย่างน้อย {n}"


def _untouched_lines(p, orig, marker):
    keep = [b for b in orig.split("\n\n\n") if marker not in b and "MAX_STOCK" not in b]
    now = p.read_text(encoding="utf-8")
    lost = [b.split("\n")[0] for b in keep if b.strip() and b.strip() not in now]
    return not lost, f"ส่วนอื่นของไฟล์หาย/ถูกแก้: {lost[:5]}"


def _rust_hidden(ws):
    tmp = ws.parent / "rust_hidden"
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(ws, tmp, ignore=shutil.ignore_patterns("target", ".office_bak"))
    lib = tmp / "src/lib.rs"
    lib.write_text(lib.read_text(encoding="utf-8") + "\n#[cfg(test)]\nmod hidden {\n    use super::*;\n    #[test]\n    fn h() {\n"
                   "        assert_eq!(fizzbuzz(15), \"FizzBuzz\");\n        assert_eq!(fizzbuzz(30), \"FizzBuzz\");\n"
                   "        assert_eq!(fizzbuzz(5), \"Buzz\");\n        assert_eq!(fizzbuzz(9), \"Fizz\");\n        assert_eq!(fizzbuzz(7), \"7\");\n    }\n}\n",
                   encoding="utf-8")
    return run("cargo test -q", tmp)
