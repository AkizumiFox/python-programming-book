#!/usr/bin/env python3
"""書本建置腳本，取代原本的 Makefile 規則。

為什麼不是 Makefile：

1. 章節 metadata 原本重複四份（`.PHONY`、`chapters:` 依賴列表、`chN:` 捷徑、個別
   target）。 加一章要改四個地方，漏改不會報錯，只會默默少編一章。 這裡改成從
   `src/book.tex` 的 `\\input{chapters/...}` 推導，book.tex 就是唯一事實來源。
2. 原本的 wrapper `.tex` 是用五條 `sed -e` 刪掉 `\\input`／`\\end{document}` 之類的行
   拼出來的。 那是拿正規表示式做結構性處理，book.tex 的寫法一變就會靜靜地失效。 這裡
   改成「保留 preamble、自己組 body」，完全不做 pattern 刪除。
3. **原本的章節 target 沒有列任何依賴**，所以 `make ch1` 只要 PDF 存在就永遠說
   "Nothing to be done"，改了 chapter.tex 也不會重編——你以為有 incremental build，
   章節部分其實完全沒有。 這裡自己比對 mtime，並且把 book.tex 與 packages/*.sty 也
   算進依賴（preamble 變了，每一章都該重編）。

用法見 `python3 scripts/build.py --help`。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRCDIR = ROOT / "src"
BUILDDIR = ROOT / "build"
OUTDIR = ROOT / "output"
CHAPTERS_OUTDIR = OUTDIR / "chapters"
BOOK_TEX = SRCDIR / "book.tex"
PACKAGES = SRCDIR / "packages"

LATEXMK = [
    "latexmk", "-xelatex", "-cd-",
    "-output-directory=.", "-aux-directory=.",
    "-latexoption=-file-line-error -halt-on-error",
    "-shell-escape", "-silent", "-interaction=nonstopmode",
    # 不要跑 biber。 sleek.sty 載了 biblatex，但這本書從頭到尾沒有一個 \cite、
    # 沒有 .bib、也沒有 \printbibliography——跑 biber 不但沒有意義，平行編譯時
    # 好幾個 biber 同時啟動還會互相踩到，冷建置時就是這樣掛掉三章的。
    # （舊的 Makefile 用同一組參數，一樣會炸；只是它的章節 target 沒有依賴，
    #   PDF 建好一次之後就再也不會重編，所以這個問題一直沒被看見。）
    "-bibtex-",
]

# CamelCase 轉可讀標題時，這些字保持小寫（第一個字除外）
_LOWER = {"and", "with", "to", "of", "for", "the", "a", "an", "in", "on"}


# --------------------------------------------------------------------------
# 章節清單：唯一事實來源是 src/book.tex 的 \input 行
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Unit:
    slug: str           # 目錄名，例如 "07_DictionariesAndSets"
    number: int | None  # 章號；front/back matter（\chapter*）是 None
    title: str          # 顯示用英文標題

    @property
    def tex(self) -> Path:
        return SRCDIR / "chapters" / self.slug / "chapter.tex"

    @property
    def pdf(self) -> Path:
        return CHAPTERS_OUTDIR / f"{self.slug}.pdf"

    @property
    def jobname(self) -> str:
        return f"chapter-{self.slug}"


def _readable_title(slug: str) -> str:
    """07_DictionariesAndSets -> 'Dictionaries and Sets'（含縮寫處理）。"""
    name = re.sub(r"^\d+_", "", slug)
    # [A-Z]+(?![a-z]) 先吃連續大寫的縮寫（CSV／API／OOP），其餘照 CamelCase 切
    words = re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|\d+", name) or [name]
    out = [words[0]]
    out += [w.lower() if w.lower() in _LOWER else w for w in words[1:]]
    return " ".join(out)


def _is_numbered(tex: Path) -> bool:
    """靠 \\chapter{ vs \\chapter*{ 判斷要不要設定章號計數器，不靠檔名猜。"""
    try:
        head = tex.read_text(encoding="utf-8")[:4000]
    except OSError:
        return True
    m = re.search(r"\\chapter(\*?)\s*[\{\[]", head)
    return bool(m) and m.group(1) != "*"


def load_units() -> list[Unit]:
    if not BOOK_TEX.exists():
        sys.exit(f"找不到 {BOOK_TEX}")
    units: list[Unit] = []
    seen: set[str] = set()
    counter = 0
    for raw in BOOK_TEX.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("%"):          # 被註解掉的 \input 不算
            continue
        # \input 與 \include 都認（舊的 sed 兩種都刪，這裡不能只認一種，
        # 否則有人改用 \include 時那一章會從清單裡靜靜消失）
        m = re.match(r"\\(?:input|include)\{chapters/([^/}]+)/chapter\}", line)
        if not m:
            continue
        slug = m.group(1)
        if slug in seen:
            sys.exit(f"book.tex 裡 {slug} 出現不只一次")
        seen.add(slug)
        u_tex = SRCDIR / "chapters" / slug / "chapter.tex"
        if not u_tex.exists():
            sys.exit(f"book.tex 指向 {slug}，但 {u_tex} 不存在")
        if _is_numbered(u_tex):
            counter += 1
            number = counter
        else:
            number = None
        units.append(Unit(slug=slug, number=number, title=_readable_title(slug)))
    if not units:
        sys.exit("book.tex 裡找不到任何 \\input{chapters/.../chapter}")
    return units


def resolve(units: list[Unit], token: str) -> Unit:
    """接受 '5' / 'ch5' / '05' / '05_Loops' / 'Loops' 幾種寫法。"""
    t = token.strip()
    m = re.fullmatch(r"(?:ch)?0*(\d+)", t, flags=re.I)
    if m:
        n = int(m.group(1))
        for u in units:
            if u.number == n:
                return u
        # 也允許用目錄前綴指 front/back matter（例如 0 -> 00_Preface、28 -> 28_References）
        for u in units:
            if re.match(rf"^0*{n}_", u.slug):
                return u
        sys.exit(f"找不到第 {n} 章")
    for u in units:
        if u.slug.lower() == t.lower():
            return u
    matches = [u for u in units if t.lower() in u.slug.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        sys.exit(f"'{token}' 對到多章：{', '.join(u.slug for u in matches)}")
    sys.exit(f"認不得 '{token}'。 用 `list` 子指令看看有哪些章。")


# --------------------------------------------------------------------------
# wrapper .tex：保留 preamble、自己組 body（不做 pattern 刪除）
# --------------------------------------------------------------------------

def preamble() -> str:
    text = BOOK_TEX.read_text(encoding="utf-8")
    idx = text.find(r"\begin{document}")
    if idx < 0:
        sys.exit(r"book.tex 裡找不到 \begin{document}")
    return text[:idx]


def write_wrapper(unit: Unit) -> Path:
    body = [preamble(), "\\begin{document}\n"]
    if unit.number is not None:
        body.append(f"\\setcounter{{chapter}}{{{unit.number - 1}}}\n")
    body.append(f"\\input{{{unit.tex.as_posix()}}}\n")
    body.append("\\end{document}\n")
    path = BUILDDIR / f"{unit.jobname}.tex"
    path.write_text("".join(body), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# 依賴與編譯
# --------------------------------------------------------------------------

_BIB_RE = re.compile(r"\\(?:cite[a-zA-Z]*|printbibliography|addbibresource|bibliography)\s*[\{\[]")


def warn_if_bibliography_appears() -> None:
    r"""`-bibtex-` 關掉了 biber。 如果哪天真的加了參考文獻，要吵，不要靜靜地漏掉。

    這本書目前零 `\cite`、沒有 .bib，跑 biber 只會在平行編譯時互相踩到（冷建置就是
    這樣掛掉三章的），所以 LATEXMK 帶了 `-bibtex-`。 但那是個會過期的前提——以後有人
    加了真正的書目卻沒改這裡，產出會少掉整份參考文獻而且完全不報錯。 這裡主動偵測。
    """
    hits = []
    for f in [BOOK_TEX, *sorted((SRCDIR / "chapters").glob("*/chapter.tex"))]:
        try:
            if _BIB_RE.search(f.read_text(encoding="utf-8")):
                hits.append(f.relative_to(ROOT))
        except OSError:
            continue
    if hits:
        print(
            "⚠️  偵測到書目相關指令，但這個腳本帶了 latexmk 的 -bibtex-（不跑 biber），\n"
            "    參考文獻不會被處理。 要啟用的話，請拿掉 build.py 裡 LATEXMK 的 "
            "\"-bibtex-\"，\n"
            "    並注意平行編譯時 biber 會互相干擾（建議搭配 -j 1）。\n"
            "    出現位置：" + "、".join(str(h) for h in hits),
            file=sys.stderr,
        )


def shared_deps() -> list[Path]:
    deps = [BOOK_TEX]
    if PACKAGES.is_dir():
        deps += sorted(PACKAGES.glob("*.sty"))
    return deps


def _publish(produced: Path, target: Path) -> None:
    """把編好的 PDF 複製到 output/，並把 mtime 設成現在。

    不能直接用 shutil.copy2：它會保留來源 mtime，而 latexmk 判斷內容沒變時不會重寫
    build/*.pdf，於是複製過去的 mtime 是舊的、永遠比依賴還舊，incremental 判斷就會
    每次都說「要重編」。 這裡明確蓋成現在時間，代表「這份產出是此刻確認過的」。
    """
    shutil.copyfile(produced, target)
    os.utime(target, None)


def needs_rebuild(target: Path, deps: list[Path]) -> bool:
    if not target.exists():
        return True
    t = target.stat().st_mtime
    return any(d.exists() and d.stat().st_mtime > t for d in deps)


# 兩種錯誤格式都要抓：預設的 "! Undefined control sequence"，以及 -file-line-error
# 打開之後的 "./chapter-18_Recursion.tex:53: File ended while scanning..."。
# （原本的 Makefile 只 grep '^!'，遇到後者會整段漏掉，只印出 log 尾巴的記憶體統計。）
_ERR_RE = re.compile(r"^(?:!|[^\s:]+\.\w+:\d+:)")


def _error_snippet(log: Path, limit: int = 40) -> str:
    if not log.exists():
        return "（沒有 log 檔可看）"
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    out: list[str] = []
    for i, line in enumerate(lines):
        if _ERR_RE.match(line):
            out += lines[i:i + 6]
            out.append("")
            if len(out) >= limit:
                break
    if out:
        return "\n".join(out[:limit])
    return "（log 裡找不到明顯的錯誤行，以下是結尾）\n" + "\n".join(lines[-15:])


def _run_latexmk(jobname: str) -> tuple[bool, str]:
    env = dict(os.environ)
    env["TEXINPUTS"] = f"{SRCDIR}:{PACKAGES}:"
    proc = subprocess.run(
        LATEXMK + [f"{jobname}.tex"],
        cwd=BUILDDIR, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if proc.returncode == 0:
        return True, ""
    return False, _error_snippet(BUILDDIR / f"{jobname}.log")


def build_unit(slug: str, number: int | None, title: str) -> tuple[str, bool, float, str]:
    """在子行程裡跑，所以參數與回傳值都用簡單型別。"""
    unit = Unit(slug=slug, number=number, title=title)
    start = time.time()
    BUILDDIR.mkdir(parents=True, exist_ok=True)
    CHAPTERS_OUTDIR.mkdir(parents=True, exist_ok=True)
    write_wrapper(unit)
    ok, err = _run_latexmk(unit.jobname)
    if ok:
        produced = BUILDDIR / f"{unit.jobname}.pdf"
        if produced.exists():
            _publish(produced, unit.pdf)
        else:
            ok, err = False, "latexmk 回報成功，但沒有產生 PDF"
    return slug, ok, time.time() - start, err


def build_book(force: bool = False) -> int:
    warn_if_bibliography_appears()
    BUILDDIR.mkdir(parents=True, exist_ok=True)
    OUTDIR.mkdir(parents=True, exist_ok=True)
    units = load_units()
    deps = shared_deps() + [u.tex for u in units]
    target = OUTDIR / "book.pdf"
    if not force and not needs_rebuild(target, deps):
        print(f"✓ {target.relative_to(ROOT)} 已是最新")
        return 0
    print("Building: 全書…")
    shutil.copy2(BOOK_TEX, BUILDDIR / "book.tex")
    start = time.time()
    ok, err = _run_latexmk("book")
    if not ok:
        print("❌ 全書編譯失敗：\n" + err, file=sys.stderr)
        return 1
    _publish(BUILDDIR / "book.pdf", target)
    print(f"✓ {target.relative_to(ROOT)}（{time.time() - start:.1f}s）")
    return 0


def build_chapters(units: list[Unit], jobs: int, force: bool) -> int:
    warn_if_bibliography_appears()
    BUILDDIR.mkdir(parents=True, exist_ok=True)
    CHAPTERS_OUTDIR.mkdir(parents=True, exist_ok=True)
    shared = shared_deps()
    todo, skipped = [], []
    for u in units:
        if force or needs_rebuild(u.pdf, shared + [u.tex]):
            todo.append(u)
        else:
            skipped.append(u)
    for u in skipped:
        print(f"·  {u.slug} 已是最新")
    if not todo:
        return 0

    failures: list[tuple[str, str]] = []
    if jobs == 1 or len(todo) == 1:
        for u in todo:
            print(f"Building: {u.title}…", flush=True)
            slug, ok, secs, err = build_unit(u.slug, u.number, u.title)
            print(f"{'✓' if ok else '❌'}  {slug}（{secs:.1f}s）")
            if not ok:
                failures.append((slug, err))
    else:
        print(f"Building {len(todo)} 章，平行度 {jobs}…", flush=True)
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            futs = {pool.submit(build_unit, u.slug, u.number, u.title): u for u in todo}
            for fut in as_completed(futs):
                slug, ok, secs, err = fut.result()
                print(f"{'✓' if ok else '❌'}  {slug}（{secs:.1f}s）", flush=True)
                if not ok:
                    failures.append((slug, err))

    if failures:
        sys.stdout.flush()
        print(f"\n❌ {len(failures)} 章失敗：", file=sys.stderr)
        for slug, err in failures:
            print(f"\n--- {slug} ---\n{err}", file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------
# clean
# --------------------------------------------------------------------------

def do_clean(also_output: bool) -> int:
    if BUILDDIR.exists():
        shutil.rmtree(BUILDDIR)
    junk = ["*.aux", "*.log", "*.bcf", "*.bbl", "*.blg", "*-SAVE-ERROR",
            "*.run.xml", "*.toc", "*.out", "*.listing", "*.xdv",
            "*.fdb_latexmk", "*.fls", "*.pdf", "*.pyg", "*.synctex.gz",
            "*.idx", "*.ind", "*.ilg"]
    for pat in junk:
        for p in SRCDIR.glob(pat):
            p.unlink(missing_ok=True)
    for p in SRCDIR.glob("chapters/*/*.aux"):
        p.unlink(missing_ok=True)
    for p in SRCDIR.glob("chapters/*/*.log"):
        p.unlink(missing_ok=True)
    for p in SRCDIR.glob("_minted-*"):
        shutil.rmtree(p, ignore_errors=True)
    print("✓ 建置暫存已清除")
    if also_output:
        if OUTDIR.exists():
            shutil.rmtree(OUTDIR)
        print("✓ output/ 已清除")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # 讓 `build.py ch5` 這種手感沿用得下去（等同 `build.py chapter ch5`）。
    # 判斷依據是「第一個不是旗標的 token 是不是已知子指令」，而插入位置一律是最前面
    # ——這樣旗標的相對順序原封不動交給子 parser 處理，`-j 2 ch5`、`ch5 --force`、
    # `--force ch5` 三種寫法都成立，不必自己解析哪個旗標會吃值。
    known = {"book", "chapters", "chapter", "all", "clean", "cleanall", "list"}
    first = next((a for a in argv if not a.startswith("-")), None)
    if first is not None and first not in known:
        argv = ["chapter"] + argv

    p = argparse.ArgumentParser(
        prog="build.py", description="《Python 入門》建置腳本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="範例：\n"
               "  build.py book            編全書\n"
               "  build.py chapters -j 8   平行編所有章節\n"
               "  build.py ch5             只編第 5 章（chapter 5 的簡寫）\n"
               "  build.py 05_Loops        用目錄名指定也可以\n"
               "  build.py list            列出 book.tex 目前收錄的章節\n",
    )
    # --force 用 parent parser 掛上去，這樣 `build.py --force ch5` 跟
    # `build.py ch5 --force` 兩種順序都吃得下（只掛在主 parser 的話，後者會被
    # argparse 當成 unrecognized argument 直接退出）。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-f", "--force", action="store_true",
                        help="忽略 mtime，強制重編")
    jobs_opt = argparse.ArgumentParser(add_help=False)
    jobs_opt.add_argument("-j", "--jobs", type=int,
                          default=min(8, os.cpu_count() or 1),
                          help="平行編譯的行程數")

    p.add_argument("-f", "--force", action="store_true", help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("book", parents=[common], help="編全書 PDF")
    sub.add_parser("chapters", parents=[common, jobs_opt], help="編所有章節 PDF")
    sp = sub.add_parser("chapter", parents=[common, jobs_opt], help="編指定章節")
    sp.add_argument("which", nargs="+")
    sub.add_parser("all", parents=[common, jobs_opt], help="全書 + 所有章節")
    sub.add_parser("clean", help="清除建置暫存")
    sub.add_parser("cleanall", help="清除建置暫存與 output/")
    sub.add_parser("list", help="列出章節")

    # 主 parser 與子 parser 都可能收到 --force，用 parse_known_args 的方式合併：
    # argparse 會讓子 parser 的值覆蓋主 parser 的，所以先自己看一眼原始 argv。
    args = p.parse_args(argv)
    if not getattr(args, "force", False):
        args.force = any(a in ("-f", "--force") for a in argv)

    if args.cmd == "clean":
        return do_clean(False)
    if args.cmd == "cleanall":
        return do_clean(True)

    units = load_units()

    if args.cmd == "list":
        for u in units:
            num = f"{u.number:>2}" if u.number is not None else " –"
            print(f"{num}  {u.slug:<34} {u.title}")
        print(f"\n共 {sum(1 for u in units if u.number is not None)} 章"
              f"（另有 {sum(1 for u in units if u.number is None)} 個不編號的單元）")
        return 0
    if args.cmd == "book":
        return build_book(args.force)
    if args.cmd == "chapters":
        return build_chapters(units, args.jobs, args.force)
    if args.cmd == "chapter":
        picked = [resolve(units, t) for t in args.which]
        return build_chapters(picked, args.jobs, args.force)
    if args.cmd == "all":
        rc = build_book(args.force)
        return build_chapters(units, args.jobs, args.force) or rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
