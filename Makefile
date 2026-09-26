.PHONY: help book chapters all clean cleanall list refs

# ---------------------------------------------------------------------------
# 這個 Makefile 現在只是一層薄薄的轉呼叫，真正的建置邏輯在 scripts/build.py。
#
# 為什麼搬走：舊版把每一章的編號、目錄名、標題重複寫在四個地方（.PHONY、chapters:
# 的依賴列表、chN: 捷徑、個別 target），加一章要改四處，漏改不會報錯只會默默少編；
# wrapper .tex 是用五條 sed 正規表示式刪行拼出來的；而且章節 target 冒號後面沒有列
# 任何依賴，所以 `make ch1` 只要 PDF 存在就永遠說 "Nothing to be done"，改了內容也
# 不會重編。 詳見 scripts/build.py 檔頭。
#
# 章節清單現在唯一的事實來源是 src/book.tex 的 \input 行，不需要在這裡維護。
# ---------------------------------------------------------------------------

PY := python3
BUILD := $(PY) scripts/build.py
JOBS ?= 8

ifdef FORCE
FORCE_FLAG := --force
endif

help:
	@echo "📚 Book Build System"
	@echo ""
	@echo "Targets:"
	@echo "  make book                - Build full book PDF"
	@echo "  make chapters            - Build all chapter PDFs (parallel)"
	@echo "  make ch[N]               - Build one chapter (e.g. make ch5)"
	@echo "  make refs                - Build the back-matter references section"
	@echo "  make all                 - Build everything"
	@echo "  make list                - List the units book.tex currently includes"
	@echo "  make clean               - Delete build artifacts"
	@echo "  make cleanall            - Delete all PDFs + artifacts"
	@echo ""
	@echo "Options:"
	@echo "  JOBS=4 make chapters     - Set parallelism (default $(JOBS))"
	@echo "  FORCE=1 make book        - Ignore timestamps and rebuild"
	@echo ""
	@echo "Everything forwards to scripts/build.py; run it directly for more"
	@echo "(e.g. '$(BUILD) --help', '$(BUILD) chapter 05_Loops')."

book:
	@$(BUILD) $(FORCE_FLAG) book

chapters:
	@$(BUILD) $(FORCE_FLAG) chapters -j $(JOBS)

all:
	@$(BUILD) $(FORCE_FLAG) all -j $(JOBS)

list:
	@$(BUILD) list

clean:
	@$(BUILD) clean

cleanall:
	@$(BUILD) cleanall

refs:
	@$(BUILD) $(FORCE_FLAG) chapter 28_References

# 一條 pattern rule 取代原本手寫的 ch1…ch27，新增章節不必再動這個檔案。
ch%:
	@$(BUILD) $(FORCE_FLAG) chapter $*
