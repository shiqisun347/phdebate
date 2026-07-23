(() => {
  "use strict";

  const root = document.documentElement;
  const documentId = root.dataset.documentId || location.pathname;
  const storageKey = `jixia:official-doc-notes:${documentId}`;
  const blocks = [...document.querySelectorAll(".noteable[data-block-id]")];
  const drawer = document.querySelector("#notes-drawer");
  const noteList = document.querySelector("#note-list");
  const countNodes = document.querySelectorAll("[data-note-count]");
  const searchInput = document.querySelector("#doc-search");
  const tocLinks = [...document.querySelectorAll(".toc a")];

  const safeParse = (value, fallback) => {
    try { return JSON.parse(value) ?? fallback; } catch { return fallback; }
  };
  const loadNotes = () => safeParse(localStorage.getItem(storageKey), []);
  const saveNotes = (notes) => localStorage.setItem(storageKey, JSON.stringify(notes));
  const id = () => globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const blockTitle = (block) => block.querySelector("h2,h3")?.textContent?.trim() || block.dataset.blockId;

  function selectedQuote(block) {
    const selection = getSelection();
    if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return "";
    const range = selection.getRangeAt(0);
    if (!block.contains(range.commonAncestorContainer)) return "";
    return selection.toString().replace(/\s+/g, " ").trim().slice(0, 360);
  }

  function addNote(block) {
    const quote = selectedQuote(block);
    const promptLabel = quote
      ? `为选中的内容添加备注：\n“${quote.slice(0, 100)}${quote.length > 100 ? "…" : ""}”`
      : `为“${blockTitle(block)}”添加备注（如需精确高亮，请先选中文字）：`;
    const content = prompt(promptLabel, "");
    if (!content?.trim()) return;
    const notes = loadNotes();
    notes.push({
      id: id(),
      blockId: block.dataset.blockId,
      blockTitle: blockTitle(block),
      quote,
      content: content.trim(),
      updatedAt: new Date().toISOString(),
    });
    saveNotes(notes);
    getSelection()?.removeAllRanges();
    render();
    drawer?.classList.add("open");
    drawer?.setAttribute("aria-hidden", "false");
  }

  function textRangeForQuote(block, quote) {
    if (!quote) return null;
    const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        return node.parentElement?.closest(".block-tools") ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
      },
    });
    const nodes = [];
    let joined = "";
    while (walker.nextNode()) {
      nodes.push({ node: walker.currentNode, start: joined.length });
      joined += walker.currentNode.textContent || "";
    }
    const needle = quote.replace(/\s+/g, " ").trim();
    const normalized = joined.replace(/\s+/g, " ");
    const normalizedIndex = normalized.indexOf(needle);
    if (normalizedIndex < 0) return null;

    // Map the normalized offset back to the source string without rewriting DOM.
    let sourceStart = 0;
    let normalizedOffset = 0;
    let inWhitespace = false;
    for (let index = 0; index < joined.length; index += 1) {
      const whitespace = /\s/.test(joined[index]);
      if (!whitespace || !inWhitespace) {
        if (normalizedOffset === normalizedIndex) { sourceStart = index; break; }
        normalizedOffset += 1;
      }
      inWhitespace = whitespace;
    }
    let sourceEnd = sourceStart;
    let targetCount = 0;
    inWhitespace = false;
    while (sourceEnd < joined.length && targetCount < needle.length) {
      const whitespace = /\s/.test(joined[sourceEnd]);
      if (!whitespace || !inWhitespace) targetCount += 1;
      inWhitespace = whitespace;
      sourceEnd += 1;
    }
    const locate = (offset) => {
      const entry = [...nodes].reverse().find((item) => item.start <= offset) || nodes[0];
      return { node: entry.node, offset: Math.max(0, Math.min(offset - entry.start, entry.node.textContent.length)) };
    };
    const start = locate(sourceStart);
    const end = locate(sourceEnd);
    const range = new Range();
    range.setStart(start.node, start.offset);
    range.setEnd(end.node, end.offset);
    return range;
  }

  function paintHighlights(notes) {
    blocks.forEach((block) => block.classList.toggle("has-notes", notes.some((note) => note.blockId === block.dataset.blockId)));
    if (!globalThis.CSS?.highlights || !globalThis.Highlight) return;
    const ranges = notes.flatMap((note) => {
      const block = document.querySelector(`.noteable[data-block-id="${CSS.escape(note.blockId)}"]`);
      const range = block ? textRangeForQuote(block, note.quote) : null;
      return range ? [range] : [];
    });
    CSS.highlights.set("doc-note", new Highlight(...ranges));
  }

  function renderNotes(notes) {
    if (!noteList) return;
    noteList.replaceChildren();
    if (!notes.length) {
      const empty = document.createElement("div");
      empty.className = "empty-notes";
      empty.textContent = "还没有备注。选中正文后点击“高亮备注”。";
      noteList.append(empty);
      return;
    }
    notes.slice().reverse().forEach((note) => {
      const card = document.createElement("article");
      card.className = "note-card";
      card.dataset.noteId = note.id;
      const title = document.createElement("div");
      title.className = "note-card-title";
      title.innerHTML = `<span></span><time></time>`;
      title.querySelector("span").textContent = note.blockTitle;
      title.querySelector("time").textContent = new Date(note.updatedAt).toLocaleDateString("zh-CN");
      card.append(title);
      if (note.quote) {
        const quote = document.createElement("div");
        quote.className = "note-quote";
        quote.textContent = `“${note.quote}”`;
        card.append(quote);
      }
      const copy = document.createElement("div");
      copy.className = "note-copy";
      copy.textContent = note.content;
      card.append(copy);
      const actions = document.createElement("div");
      actions.className = "note-card-actions";
      const jump = document.createElement("button");
      jump.type = "button";
      jump.textContent = "定位";
      jump.addEventListener("click", () => {
        document.querySelector(`.noteable[data-block-id="${CSS.escape(note.blockId)}"]`)?.scrollIntoView({ behavior: "smooth", block: "start" });
        drawer?.classList.remove("open");
      });
      const edit = document.createElement("button");
      edit.type = "button";
      edit.textContent = "编辑";
      edit.addEventListener("click", () => {
        const next = prompt("编辑备注：", note.content);
        if (!next?.trim()) return;
        const all = loadNotes();
        const target = all.find((item) => item.id === note.id);
        if (target) {
          target.content = next.trim();
          target.updatedAt = new Date().toISOString();
          saveNotes(all);
          render();
        }
      });
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "danger";
      remove.textContent = "删除";
      remove.addEventListener("click", () => {
        if (!confirm("删除这条本机备注？")) return;
        saveNotes(loadNotes().filter((item) => item.id !== note.id));
        render();
      });
      actions.append(jump, edit, remove);
      card.append(actions);
      noteList.append(card);
    });
  }

  function render() {
    const notes = loadNotes();
    countNodes.forEach((node) => { node.textContent = String(notes.length); });
    paintHighlights(notes);
    renderNotes(notes);
  }

  blocks.forEach((block) => {
    const tools = document.createElement("div");
    tools.className = "block-tools";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "note-button";
    button.textContent = "高亮备注";
    button.title = "先选中文字可建立精确高亮；不选择则备注整个章节";
    button.addEventListener("click", () => addNote(block));
    tools.append(button);
    block.prepend(tools);
  });

  document.querySelector("#open-notes")?.addEventListener("click", () => {
    drawer?.classList.add("open");
    drawer?.setAttribute("aria-hidden", "false");
    drawer?.querySelector("button")?.focus();
  });
  document.querySelector("#close-notes")?.addEventListener("click", () => {
    drawer?.classList.remove("open");
    drawer?.setAttribute("aria-hidden", "true");
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && drawer?.classList.contains("open")) {
      drawer.classList.remove("open");
      drawer.setAttribute("aria-hidden", "true");
      document.querySelector("#open-notes")?.focus();
    }
  });
  document.querySelector("#export-notes")?.addEventListener("click", () => {
    const payload = JSON.stringify({ documentId, exportedAt: new Date().toISOString(), notes: loadNotes() }, null, 2);
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([payload], { type: "application/json" }));
    link.download = `${documentId}-notes.json`;
    link.click();
    URL.revokeObjectURL(link.href);
  });
  document.querySelector("#clear-notes")?.addEventListener("click", () => {
    if (!loadNotes().length || !confirm("清空这份文档在本机浏览器中的全部备注？")) return;
    localStorage.removeItem(storageKey);
    render();
  });

  searchInput?.addEventListener("input", () => {
    const query = searchInput.value.trim().toLocaleLowerCase("zh-CN");
    let visible = 0;
    blocks.forEach((block) => {
      const matches = !query || block.textContent.toLocaleLowerCase("zh-CN").includes(query);
      block.classList.toggle("search-hidden", !matches);
      if (matches) visible += 1;
    });
    document.querySelector("#search-empty")?.classList.toggle("visible", visible === 0);
  });

  if ("IntersectionObserver" in globalThis) {
    const observer = new IntersectionObserver((entries) => {
      const visible = entries.filter((entry) => entry.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
      if (!visible) return;
      tocLinks.forEach((link) => link.classList.toggle("active", link.hash === `#${visible.target.id}`));
    }, { rootMargin: "-15% 0px -75% 0px" });
    blocks.forEach((block) => observer.observe(block));
  }

  render();
})();
