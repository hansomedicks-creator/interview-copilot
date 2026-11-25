(() => {
  const panel = document.getElementById("resume-preview");
  const frame = document.getElementById("resume-preview-frame");
  const message = document.getElementById("resume-preview-message");
  let blobUrl = null;
  let generation = 0;
  function close() {
    generation += 1;
    panel.classList.add("hidden");
    frame.src = "about:blank";
    if (blobUrl) URL.revokeObjectURL(blobUrl);
    blobUrl = null;
  }
  async function open(interviewId, name) {
    close();
    const current = generation;
    panel.classList.remove("hidden");
    document.getElementById("resume-preview-title").textContent = `${name || "候选人"} · PDF 简历`;
    message.textContent = "正在加载 PDF…";
    frame.classList.add("hidden");
    try {
      const response = await fetch(`/api/v1/interviews/${encodeURIComponent(interviewId)}/resume-document`, { cache: "no-store" });
      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || "简历原件加载失败");
      }
      const blob = await response.blob();
      if (current !== generation) return;
      blobUrl = URL.createObjectURL(blob);
      frame.src = blobUrl;
      frame.classList.remove("hidden");
      message.textContent = "拖动标题栏移动，拖动右下角调整大小。";
    } catch (error) { if (current === generation) message.textContent = error.message; }
  }
  async function upload(interviewId, file) {
    if (!file.name.toLowerCase().endsWith(".pdf")) throw new Error("请选择 PDF 简历");
    if (file.size > 10 * 1024 * 1024) throw new Error("PDF 不能超过 10MB");
    const response = await fetch(`/api/v1/interviews/${encodeURIComponent(interviewId)}/resume-document`, {
      method: "PUT", headers: { "Content-Type": "application/pdf", "X-Filename": encodeURIComponent(file.name) }, body: file,
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
