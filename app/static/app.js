(() => {
  const meta = document.querySelector('meta[name="csrf-token"]');
  window.csrfToken = meta ? meta.content : "";

  window.apiFetch = async (url, options = {}) => {
    const headers = new Headers(options.headers || {});
    if (
      options.body &&
      !headers.has("Content-Type") &&
      !(options.body instanceof FormData)
    ) {
      headers.set("Content-Type", "application/json");
    }
    if (window.csrfToken) headers.set("X-CSRF-Token", window.csrfToken);
    const response = await fetch(url, {
      credentials: "same-origin",
      ...options,
      headers,
    });
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json")
      ? await response.json()
      : await response.text();
    if (!response.ok) {
      const detail = typeof payload === "object" ? payload.detail : payload;
      throw new Error(detail || `HTTP ${response.status}`);
    }
    return payload;
  };

  const runAgent = document.querySelector("#run-agent");
  if (runAgent) {
    runAgent.addEventListener("click", async () => {
      const ticketId = runAgent.dataset.ticketId;
      const original = runAgent.textContent;
      runAgent.disabled = true;
      runAgent.textContent = "分析中…";
      try {
        await window.apiFetch(`/api/tickets/${ticketId}/process`, {
          method: "POST",
        });
        window.location.reload();
      } catch (error) {
        runAgent.disabled = false;
        runAgent.textContent = original;
        window.alert(`Agent 分析失败：${error.message}`);
      }
    });
  }

  const withBusy = async (button, label, task) => {
    const original = button.textContent;
    button.disabled = true;
    button.textContent = label;
    try {
      await task();
      window.location.reload();
    } catch (error) {
      button.disabled = false;
      button.textContent = original;
      window.alert(error.message);
    }
  };

  const uploadForm = document.querySelector("#upload-attachment-form");
  if (uploadForm) {
    uploadForm.addEventListener("submit", (event) => {
      event.preventDefault();
      const fileInput = uploadForm.querySelector("#attachment-file");
      if (!fileInput.files.length) {
        window.alert("请选择要上传的文件");
        return;
      }
      const data = new FormData();
      data.append("file", fileInput.files[0]);
      const button = uploadForm.querySelector("button");
      withBusy(button, "上传中…", () =>
        window.apiFetch(
          `/api/tickets/${uploadForm.dataset.ticketId}/attachments`,
          { method: "POST", body: data }
        )
      );
    });
  }

  document.querySelectorAll(".ocr-attachment").forEach((button) => {
    button.addEventListener("click", async () => {
      const row = document.querySelector(
        `.ocr-result[data-attachment-id="${button.dataset.attachmentId}"]`
      );
      const target = row ? row.querySelector(".ocr-text") : null;
      const original = button.textContent;
      button.disabled = true;
      button.textContent = "识别中…";
      try {
        const result = await window.apiFetch(
          `/api/tickets/${button.dataset.ticketId}/attachments/${button.dataset.attachmentId}/ocr`,
          { method: "POST" }
        );
        if (row && target) {
          row.hidden = false;
          target.textContent = `[${result.provider}] ${result.text}`;
        }
      } catch (error) {
        window.alert(`OCR 失败：${error.message}`);
      } finally {
        button.disabled = false;
        button.textContent = original;
      }
    });
  });

  const publishIndex = document.querySelector("#publish-index");
  if (publishIndex) {
    publishIndex.addEventListener("click", () => {
      const versionIds = Array.from(
        document.querySelectorAll('input[name="version_ids"]:checked')
      ).map((input) => input.value);
      if (!versionIds.length) {
        window.alert("请选择至少一个已批准版本");
        return;
      }
      withBusy(publishIndex, "发布中…", () =>
        window.apiFetch("/api/knowledge/index/publish", {
          method: "POST",
          body: JSON.stringify({ version_ids: versionIds }),
        })
      );
    });
  }

  document.querySelectorAll(".rollback-index").forEach((button) => {
    button.addEventListener("click", () => {
      if (!window.confirm("回滚到该快照？")) return;
      withBusy(button, "回滚中…", () =>
        window.apiFetch("/api/knowledge/index/rollback", {
          method: "POST",
          body: JSON.stringify({ snapshot_id: button.dataset.snapshotId }),
        })
      );
    });
  });

  const approveButton = document.querySelector("#approve-draft");
  if (approveButton) {
    approveButton.addEventListener("click", () => {
      const ticketId = approveButton.dataset.ticketId;
      const draftId = approveButton.dataset.draftId;
      withBusy(approveButton, "批准中…", () =>
        window.apiFetch(
          `/api/tickets/${ticketId}/drafts/${draftId}/approve`,
          {
            method: "POST",
            body: JSON.stringify({
              content_hash: approveButton.dataset.contentHash,
            }),
          }
        )
      );
    });
  }

  const saveDraft = document.querySelector("#save-draft");
  if (saveDraft) {
    saveDraft.addEventListener("click", () => {
      const ticketId = saveDraft.dataset.ticketId;
      const body = document.querySelector("#draft-edit-body").value;
      withBusy(saveDraft, "保存中…", () =>
        window.apiFetch(`/api/tickets/${ticketId}/drafts`, {
          method: "POST",
          body: JSON.stringify({ body }),
        })
      );
    });
  }

  const rejectButton = document.querySelector("#reject-draft");
  if (rejectButton) {
    rejectButton.addEventListener("click", () => {
      const note = window.prompt("驳回原因（可选）") || null;
      const ticketId = rejectButton.dataset.ticketId;
      const draftId = rejectButton.dataset.draftId;
      withBusy(rejectButton, "驳回中…", () =>
        window.apiFetch(`/api/tickets/${ticketId}/drafts/${draftId}/reject`, {
          method: "POST",
          body: JSON.stringify({ note }),
        })
      );
    });
  }

  document.querySelectorAll(".followup-form").forEach((form) => {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const ticketId = form.dataset.ticketId;
      const followupId = form.dataset.followupId;
      const button = form.querySelector("button");
      withBusy(button, "保存中…", () =>
        window.apiFetch(
          `/api/tickets/${ticketId}/followups/${followupId}`,
          {
            method: "POST",
            body: JSON.stringify({
              conclusion: form.elements.conclusion.value,
              basis: form.elements.basis.value,
              notified: form.elements.notified.checked,
            }),
          }
        )
      );
    });
  });

  const runDiagnosis = document.querySelector("#run-diagnosis");
  if (runDiagnosis) {
    runDiagnosis.addEventListener("click", () => {
      const ticketId = runDiagnosis.dataset.ticketId;
      withBusy(runDiagnosis, "诊断中…", () =>
        window.apiFetch(`/api/tickets/${ticketId}/diagnose`, { method: "POST" })
      );
    });
  }

  const diagnosisForm = document.querySelector("#apply-diagnosis-form");
  if (diagnosisForm) {
    diagnosisForm.addEventListener("submit", (event) => {
      event.preventDefault();
      const ticketId = diagnosisForm.dataset.ticketId;
      const button = diagnosisForm.querySelector("button");
      const followUp = diagnosisForm.elements.next_follow_up_at.value;
      withBusy(button, "写回中…", () =>
        window.apiFetch(`/api/tickets/${ticketId}/diagnose/apply`, {
          method: "POST",
          body: JSON.stringify({
            diagnosis_id: diagnosisForm.dataset.diagnosisId,
            next_step: diagnosisForm.elements.next_step.value,
            next_owner_id: diagnosisForm.elements.next_owner_id.value || null,
            next_follow_up_at: followUp
              ? new Date(followUp).toISOString()
              : null,
          }),
        })
      );
    });
  }

  const trainingForm = document.querySelector("#training-form");
  if (trainingForm) {
    trainingForm.addEventListener("submit", (event) => {
      event.preventDefault();
      const caseId = trainingForm.dataset.caseId;
      const attemptId = trainingForm.dataset.attemptId;
      const button = trainingForm.querySelector("button");
      const answer = document.querySelector("#training-answer").value;
      const url = attemptId
        ? `/api/training/cases/${caseId}/attempts/${attemptId}/revise`
        : `/api/training/cases/${caseId}/attempts`;
      withBusy(button, "提交中…", () =>
        window.apiFetch(url, {
          method: "POST",
          body: JSON.stringify({ answer }),
        })
      );
    });
  }

  const closeForm = document.querySelector("#close-ticket-form");
  if (closeForm) {
    closeForm.addEventListener("submit", (event) => {
      event.preventDefault();
      const ticketId = closeForm.dataset.ticketId;
      const button = closeForm.querySelector("button");
      withBusy(button, "结案中…", () =>
        window.apiFetch(`/api/tickets/${ticketId}/close`, {
          method: "POST",
          body: JSON.stringify({
            conclusion: closeForm.elements.conclusion.value,
            basis: closeForm.elements.basis.value,
            notified: closeForm.elements.notified.checked,
          }),
        })
      );
    });
  }

  const employeeTable = document.querySelector("#employee-table");
  if (employeeTable) {
    const runAdmin = (control, task) => {
      control.disabled = true;
      task()
        .then(() => window.location.reload())
        .catch((error) => {
          window.alert(error.message);
          window.location.reload();
        });
    };
    const patchEmployee = (employeeId, body) =>
      window.apiFetch(`/api/admin/employees/${employeeId}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      });

    document.querySelectorAll(".employee-role").forEach((select) => {
      select.addEventListener("change", () =>
        runAdmin(select, () =>
          patchEmployee(select.dataset.employeeId, { role: select.value })
        )
      );
    });

    document.querySelectorAll(".employee-refund").forEach((box) => {
      box.addEventListener("change", () =>
        runAdmin(box, () =>
          patchEmployee(box.dataset.employeeId, {
            refund_authority: box.checked,
          })
        )
      );
    });

    document.querySelectorAll(".employee-active").forEach((box) => {
      box.addEventListener("change", () =>
        runAdmin(box, () =>
          patchEmployee(box.dataset.employeeId, { active: box.checked })
        )
      );
    });

    document.querySelectorAll(".grant-add").forEach((button) => {
      button.addEventListener("click", () => {
        const row = button.closest("tr");
        const select = row.querySelector(".grant-store");
        if (!select.value) {
          window.alert("请选择要授权的店铺");
          return;
        }
        runAdmin(button, () =>
          window.apiFetch(
            `/api/admin/employees/${button.dataset.employeeId}/grants`,
            {
              method: "POST",
              body: JSON.stringify({ store_id: select.value }),
            }
          )
        );
      });
    });

    document.querySelectorAll(".grant-revoke").forEach((button) => {
      button.addEventListener("click", () => {
        if (!window.confirm("回收该店铺授权？")) return;
        runAdmin(button, () =>
          window.apiFetch(
            `/api/admin/employees/${button.dataset.employeeId}/grants/${button.dataset.storeId}`,
            { method: "DELETE" }
          )
        );
      });
    });
  }
})();