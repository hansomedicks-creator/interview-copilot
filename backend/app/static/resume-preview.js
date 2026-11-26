(() => {
  const panel = document.getElementById("resume-preview");
  const frame = document.getElementById("resume-preview-frame");
  const message = document.getElementById("resume-preview-message");
  const title = document.getElementById("resume-preview-title");
  const word = document.getElementById("resume-word-page");
  const navigation = document.getElementById("resume-word-nav");
  const previous = document.getElementById("resume-word-prev");
  const next = document.getElementById("resume-word-next");
  const count = document.getElementById("resume-word-count");
  let blobUrl = null;
  let generation = 0;
  let pages = [];
  let page = 0;
  function flip(index) {
    if (index < 0 || index >= pages.length) return;
    page = index;
    word.textContent = pages[page];
    word.scrollTop = 0;
    count.textContent = `第 ${page + 1} / ${pages.length} 页`;
    previous.disabled = page === 0;
    next.disabled = page === pages.length - 1;
  }
  previous.addEventListener("click", () => flip(page - 1));
  next.addEventListener("click", () => flip(page + 1));
  function close() {
    generation += 1;
    panel.classList.add("hidden");
    frame.src = "about:blank";
    frame.classList.add("hidden");
    word.classList.add("hidden");
    navigation.classList.add("hidden");
    word.textContent = "";
    pages = [];
    if (blobUrl) URL.revokeObjectURL(blobUrl);
    blobUrl = null;
  }
  async function open(interviewId, name) {
    close();
    const current = generation;
    panel.classList.remove("hidden");
    title.textContent = `${name || "候选人"} · 简历`;
    message.textContent = "正在加载简历…";
    try {
      const response = await fetch(`/api/v1/interviews/${encodeURIComponent(interviewId)}/resume-document`, { cache: "no-store" });
      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || "简历原件加载失败");
      }
      if ((response.headers.get("content-type") || "").includes("application/json")) {
        const data = await response.json();
        if (current !== generation) return;
        if (data.format !== "docx" || !data.pages?.length) throw new Error("没有可预览的 Word 正文");
        pages = data.pages;
        title.textContent = `${name || "候选人"} · Word 简历`;
        flip(0);
        word.classList.remove("hidden");
        navigation.classList.remove("hidden");
        message.textContent = "Word 正文已重新分页；不显示图片或原始排版。";
        return;
      }
      const blob = await response.blob();
      if (current !== generation) return;
      if (blob.type !== "application/pdf") throw new Error("暂不支持该简历格式");
      blobUrl = URL.createObjectURL(blob);
      frame.src = blobUrl;
      frame.classList.remove("hidden");
      message.textContent = "拖动标题栏移动，拖动右下角调整大小。";
    } catch (error) { if (current === generation) message.textContent = error.message; }
  }
  async function upload(interviewId, file) {
    const isWord = file.name.toLowerCase().endsWith(".docx");
    if (!isWord && !file.name.toLowerCase().endsWith(".pdf")) throw new Error("请选择 PDF 或 .docx 简历");
    const maxMb = isWord ? 20 : 10;
    if (file.size > maxMb * 1024 * 1024) throw new Error(`简历不能超过 ${maxMb} MB`);
    const response = await fetch(`/api/v1/interviews/${encodeURIComponent(interviewId)}/resume-document`, {
      method: "PUT", headers: { "Content-Type": "application/octet-stream", "X-Filename": encodeURIComponent(file.name) }, body: file,
    });
    if (!response.ok) throw new Error((await response.json()).detail || "PDF 保存失败");
  }
  const handle = document.getElementById("resume-preview-handle");
  handle.addEventListener("pointerdown", event => {
    if (event.target.closest("button")) return;
    const rect = panel.getBoundingClientRect();
    const dx = event.clientX - rect.left, dy = event.clientY - rect.top;
    handle.setPointerCapture(event.pointerId);
    const move = e => {
      panel.style.right = "auto";
      panel.style.left = `${Math.max(0, Math.min(window.innerWidth - panel.offsetWidth, e.clientX - dx))}px`;
      panel.style.top = `${Math.max(0, Math.min(window.innerHeight - 50, e.clientY - dy))}px`;
    };
    const end = () => { handle.removeEventListener("pointermove", move); handle.removeEventListener("lostpointercapture", end); };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("lostpointercapture", end);
  });
  document.getElementById("resume-preview-close").addEventListener("click", close);
  panel.addEventListener("keydown", e => { if (e.key === "Escape") close(); });
  window.addEventListener("resize", () => { panel.style.left = "auto"; panel.style.right = "12px"; panel.style.top = "70px"; });
  window.resumePreview = { open, close, upload };
})();
