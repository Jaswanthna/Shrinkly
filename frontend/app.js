const fileInput = document.querySelector("#file-input");
const dropzone = document.querySelector("#dropzone");
const fileSummary = document.querySelector("#file-summary");
const startButton = document.querySelector("#start-button");
const modeInput = document.querySelector("#mode");
const outputPreview = document.querySelector("#output-preview");
const targetInput = document.querySelector("#target-ai");
const targetAiField = document.querySelector("#target-ai-field");
const customLimitField = document.querySelector("#custom-limit-field");
const customLimitInput = document.querySelector("#custom-limit");
const languageInput = document.querySelector("#ocr-language");
const ocrLanguageField = document.querySelector("#ocr-language-field");
const progressArea = document.querySelector("#progress-area");
const progressBar = document.querySelector("#progress");
const progressPercent = document.querySelector("#progress-percent");
const progressLabel = document.querySelector("#progress-label");
const analysisSummary = document.querySelector("#analysis");
const warningList = document.querySelector("#warnings");
const downloadLink = document.querySelector("#download-link");
const errorMessage = document.querySelector("#error-message");

const MAX_FILE_BYTES = 1024 * 1024 * 1024;
let selectedFile = null;
let busy = false;

function formatBytes(bytes) {
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function setProgress(value, label) {
  const bounded = Math.max(0, Math.min(100, Math.round(value)));
  progressBar.value = bounded;
  progressPercent.textContent = `${bounded}%`;
  progressLabel.textContent = label;
}

function selectFile(file) {
  if (!file) return;
  errorMessage.textContent = "";
  if (file.size > MAX_FILE_BYTES) {
    selectedFile = null;
    fileSummary.hidden = false;
    fileSummary.textContent = "That file is larger than the 1 GB upload limit.";
    startButton.disabled = true;
    return;
  }
  selectedFile = file;
  fileSummary.hidden = false;
  fileSummary.textContent = `${file.name} · ${formatBytes(file.size)}`;
  startButton.disabled = busy;
}

fileInput.addEventListener("change", () => selectFile(fileInput.files?.[0]));

for (const eventName of ["dragenter", "dragover"]) {
  dropzone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropzone.classList.add("drag-over");
  });
}

for (const eventName of ["dragleave", "drop"]) {
  dropzone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropzone.classList.remove("drag-over");
  });
}

dropzone.addEventListener("drop", (event) => {
  selectFile(event.dataTransfer?.files?.[0]);
});

targetInput.addEventListener("change", () => {
  customLimitField.hidden = targetInput.value !== "custom";
});

function updateModeOptions() {
  const mode = modeInput.value;
  targetAiField.hidden = mode !== "split";
  customLimitField.hidden = mode !== "split" || targetInput.value !== "custom";
  ocrLanguageField.hidden = mode === "compressed_original";

  if (mode === "text_only") {
    outputPreview.innerHTML =
      "Download: <b>ai-ready.md</b> — one complete Markdown document.";
  } else if (mode === "compressed_original") {
    outputPreview.innerHTML =
      "Download: <b>compressed.pdf</b> — one compressed PDF, no extra files.";
  } else {
    outputPreview.innerHTML =
      "Download: <b>ai-ready-parts.zip</b> with parts and a manifest — no duplicate full document.";
  }
}

modeInput.addEventListener("change", updateModeOptions);
updateModeOptions();

async function apiRequest(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let message = `Request failed (${response.status}).`;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") message = body.detail;
    } catch {
      // Keep the HTTP status message if the server response is not JSON.
    }
    throw new Error(message);
  }
  return response;
}

async function uploadFile(file) {
  const initResponse = await apiRequest("/api/upload/init", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename: file.name, file_size: file.size }),
  });
  const upload = await initResponse.json();
  for (let index = 0; index < upload.total_chunks; index += 1) {
    const start = index * upload.chunk_size;
    const chunk = file.slice(start, Math.min(start + upload.chunk_size, file.size));
    await apiRequest(
      `/api/upload/${encodeURIComponent(upload.upload_id)}/chunk?chunk_index=${index}`,
      { method: "PUT", headers: { "Content-Type": "application/octet-stream" }, body: chunk },
    );
    setProgress(((index + 1) / upload.total_chunks) * 35, "Uploading your PDF…");
  }
  return upload.upload_id;
}

async function waitForJob(jobId) {
  while (true) {
    await new Promise((resolve) => window.setTimeout(resolve, 900));
    const response = await apiRequest(`/api/jobs/${encodeURIComponent(jobId)}`);
    const job = await response.json();
    setProgress(35 + job.progress * 0.65, "Analyzing and preparing your document…");
    renderJob(job);
    if (job.status === "completed") return;
    if (job.status === "failed") throw new Error(job.error || "The conversion failed.");
    if (job.status === "deleting") throw new Error("This job was deleted.");
  }
}

function renderJob(job) {
  warningList.replaceChildren();
  for (const warning of job.warnings || []) {
    const item = document.createElement("li");
    item.textContent = warning;
    warningList.append(item);
  }
  if (job.analysis) {
    const scanned = job.analysis.pages.filter((page) => page.scanned).length;
    analysisSummary.hidden = false;
    analysisSummary.textContent =
      `${job.analysis.page_count} pages · ${scanned} scanned · ` +
      `about ${job.analysis.estimated_tokens.toLocaleString()} source tokens`;
  }
}

startButton.addEventListener("click", async () => {
  if (!selectedFile || busy) return;
  busy = true;
  startButton.disabled = true;
  progressArea.hidden = false;
  downloadLink.hidden = true;
  analysisSummary.hidden = true;
  warningList.replaceChildren();
  errorMessage.textContent = "";
  setProgress(0, "Preparing your file…");

  try {
    const uploadId = await uploadFile(selectedFile);
    const jobPayload = {
      upload_id: uploadId,
      mode: modeInput.value,
      target_ai: modeInput.value === "split" ? targetInput.value : "chatgpt",
      ocr_language: languageInput.value.trim(),
    };
    if (modeInput.value === "split" && targetInput.value === "custom") {
      jobPayload.custom_token_limit = Number(customLimitInput.value);
    }
    const createResponse = await apiRequest("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(jobPayload),
    });
    const { job_id: jobId } = await createResponse.json();
    await waitForJob(jobId);
    downloadLink.href = `/api/jobs/${encodeURIComponent(jobId)}/download`;
    downloadLink.textContent = `Download ${modeInput.value === "text_only" ? "Markdown file" : modeInput.value === "compressed_original" ? "compressed PDF" : "parts ZIP"}`;
    downloadLink.hidden = false;
    setProgress(100, "Your AI-ready file is ready.");
  } catch (error) {
    errorMessage.textContent = error instanceof Error ? error.message : "Something went wrong.";
    progressLabel.textContent = "We couldn't finish this file.";
  } finally {
    busy = false;
    startButton.disabled = !selectedFile;
  }
});
