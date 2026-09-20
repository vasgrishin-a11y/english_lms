/* English LMS — прогрессивное улучшение интерфейса.
 * Работает без сборки, не требует htmx и не ломает сценарии без JavaScript:
 * автосохранение черновика, горячие клавиши проверки, карточки тренажёра,
 * запись аудио в браузере, вставка шаблонов комментариев.
 */
(function () {
  "use strict";

  var ready = function (fn) {
    if (document.readyState !== "loading") {
      fn();
    } else {
      document.addEventListener("DOMContentLoaded", fn);
    }
  };

  function csrfToken() {
    var body = document.body;
    if (body && body.getAttribute("hx-headers")) {
      try {
        var headers = JSON.parse(body.getAttribute("hx-headers"));
        if (headers["X-CSRFToken"]) return headers["X-CSRFToken"];
      } catch (error) {
        /* заголовок не задан — берём из cookie */
      }
    }
    var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  function isTypingTarget(target) {
    if (!target) return false;
    var tag = target.tagName;
    return (
      tag === "INPUT" ||
      tag === "TEXTAREA" ||
      tag === "SELECT" ||
      target.isContentEditable === true
    );
  }

  /* ── Сообщения: мягкое скрытие без потери для скринридеров ─────────────── */
  function autohideAlerts() {
    var alerts = document.querySelectorAll("[data-autohide]");
    Array.prototype.forEach.call(alerts, function (alert) {
      var delay = parseInt(alert.getAttribute("data-autohide"), 10) || 6000;
      window.setTimeout(function () {
        alert.classList.add("is-fading");
        window.setTimeout(function () {
          if (alert.parentNode) alert.parentNode.removeChild(alert);
        }, 320);
      }, delay);
    });
  }

  /* ── Подтверждение разрушающих действий ─────────────────────────────────── */
  function confirmForms() {
    document.addEventListener("submit", function (event) {
      var form = event.target.closest ? event.target.closest("[data-confirm]") : null;
      if (!form) return;
      if (form.dataset.confirmed === "1") return;
      event.preventDefault();
      if (window.confirm(form.getAttribute("data-confirm"))) {
        form.dataset.confirmed = "1";
        form.submit();
      }
    });
  }

  /* ── Автосохранение черновика ответа ────────────────────────────────────── */
  function draftAutosave() {
    var form = document.querySelector("form[data-draft-url]");
    if (!form) return;
    var area = form.querySelector("#id_text_answer");
    if (!area) return;
    var target = document.querySelector(form.getAttribute("data-draft-target") || "");
    var url = form.getAttribute("data-draft-url");
    var tokenInput = form.querySelector("input[name=csrfmiddlewaretoken]");
    var timer = null;
    var pending = null;
    var lastSaved = area.value;

    function setStatus(text) {
      if (!target) return;
      target.textContent = text;
    }

    function save() {
      var text = area.value;
      if (text === lastSaved) return;
      var body = new URLSearchParams();
      body.set("text", text);
      if (tokenInput) body.set("csrfmiddlewaretoken", tokenInput.value);
      pending = fetch(url, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "X-CSRFToken": csrfToken(),
          "X-Requested-With": "XMLHttpRequest",
          "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
        },
        body: body.toString()
      })
        .then(function (response) {
          if (!response.ok) throw new Error("HTTP " + response.status);
          return response.text();
        })
        .then(function (html) {
          lastSaved = text;
          if (target) target.innerHTML = html;
        })
        .catch(function () {
          setStatus("Черновик не сохранён — проверьте соединение");
        })
        .finally(function () {
          pending = null;
          if (area.value !== lastSaved) schedule();
        });
    }

    function schedule() {
      window.clearTimeout(timer);
      timer = window.setTimeout(save, 1200);
    }

    area.addEventListener("input", schedule);
    form.addEventListener("submit", function () {
      window.clearTimeout(timer);
    });
    window.addEventListener("beforeunload", function (event) {
      if (pending) {
        event.preventDefault();
        event.returnValue = "";
      }
    });
  }

  /* ── Карточки тренажёра: переворот и оценки с клавиатуры ────────────────── */
  function flashcards() {
    var card = document.querySelector("[data-flashcard]");
    if (!card) return;

    function flip() {
      card.classList.toggle("is-flipped");
      var shown = card.classList.contains("is-flipped");
      card.setAttribute("aria-pressed", shown ? "true" : "false");
    }

    card.setAttribute("aria-pressed", "false");
    card.addEventListener("click", flip);

    document.addEventListener("keydown", function (event) {
      if (isTypingTarget(event.target) || event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.key === " " || event.code === "Space") {
        event.preventDefault();
        flip();
        return;
      }
      var ratings = { 1: "again", 2: "hard", 3: "good", 4: "easy" };
      var rating = ratings[event.key];
      if (!rating) return;
      var button = document.querySelector(
        'form[action*="/trainer/"] button[name="rating"][value="' + rating + '"]'
      );
      if (button) {
        event.preventDefault();
        button.click();
      }
    });
  }

  /* ── Запись аудио в браузере ────────────────────────────────────────────── */
  function audioRecorder() {
    var panel = document.querySelector("[data-recorder]");
    if (!panel) return;
    var toggle = panel.querySelector("[data-recorder-toggle]");
    var stop = panel.querySelector("[data-recorder-stop]");
    var status = panel.querySelector("[data-recorder-status]");
    var preview = panel.querySelector("[data-recorder-preview]");
    var errorBox = panel.querySelector("[data-recorder-error]");
    var input = document.getElementById(panel.getAttribute("data-input") || "");
    if (!toggle || !input) return;

    var recorder = null;
    var chunks = [];
    var stream = null;

    function fail(message) {
      if (errorBox) errorBox.textContent = message;
      if (status) status.textContent = "Запись недоступна";
      reset();
    }

    function reset() {
      toggle.classList.remove("hidden");
      if (stop) stop.classList.add("hidden");
      if (stream) {
        stream.getTracks().forEach(function (track) {
          track.stop();
        });
        stream = null;
      }
      recorder = null;
      chunks = [];
    }

    function encodeWav(blob, callback) {
      var reader = new FileReader();
      reader.onload = function () {
        var context = new (window.AudioContext || window.webkitAudioContext)();
        context
          .decodeAudioData(reader.result)
          .then(function (buffer) {
            var channels = buffer.numberOfChannels;
            var length = buffer.length;
            var sampleRate = buffer.sampleRate;
            var bytes = 44 + length * channels * 2;
            var view = new DataView(new ArrayBuffer(bytes));
            var writeText = function (offset, text) {
              for (var i = 0; i < text.length; i += 1) view.setUint8(offset + i, text.charCodeAt(i));
            };
            writeText(0, "RIFF");
            view.setUint32(4, bytes - 8, true);
            writeText(8, "WAVE");
            writeText(12, "fmt ");
            view.setUint32(16, 16, true);
            view.setUint16(20, 1, true);
            view.setUint16(22, channels, true);
            view.setUint32(24, sampleRate, true);
            view.setUint32(28, sampleRate * channels * 2, true);
            view.setUint16(32, channels * 2, true);
            view.setUint16(34, 16, true);
            writeText(36, "data");
            view.setUint32(40, length * channels * 2, true);
            var offset = 44;
            for (var i = 0; i < length; i += 1) {
              for (var channel = 0; channel < channels; channel += 1) {
                var sample = Math.max(-1, Math.min(1, buffer.getChannelData(channel)[i]));
                view.setInt16(offset, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
                offset += 2;
              }
            }
            if (context.close) context.close();
            callback(new Blob([view.buffer], { type: "audio/wav" }));
          })
          .catch(function () {
            callback(blob);
          });
      };
      reader.readAsArrayBuffer(blob);
    }

    function attach(blob) {
      var name = "zapis-otveta-" + new Date().toISOString().slice(0, 19).replace(/[:T]/g, "") + ".wav";
      var file = new File([blob], name, { type: "audio/wav" });
      try {
        var transfer = new DataTransfer();
        transfer.items.add(file);
        input.files = transfer.files;
      } catch (error) {
        fail("Браузер не позволяет подставить файл автоматически — скачайте запись и прикрепите её вручную.");
        return;
      }
      if (preview) {
        preview.src = URL.createObjectURL(blob);
        preview.classList.remove("hidden");
      }
      if (status) status.textContent = "Запись готова: " + name;
      if (errorBox) errorBox.textContent = "";
      input.dispatchEvent(new Event("change", { bubbles: true }));
    }

    toggle.addEventListener("click", function () {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        fail("Браузер не поддерживает запись звука. Прикрепите готовый аудиофайл.");
        return;
      }
      if (errorBox) errorBox.textContent = "";
      if (status) status.textContent = "Запрашиваем микрофон…";
      navigator.mediaDevices
        .getUserMedia({ audio: true })
        .then(function (mediaStream) {
          stream = mediaStream;
          chunks = [];
          var mime = window.MediaRecorder && MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm" : "";
          recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
          recorder.ondataavailable = function (event) {
            if (event.data && event.data.size) chunks.push(event.data);
          };
          recorder.onstop = function () {
            var blob = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
            encodeWav(blob, attach);
            reset();
          };
          recorder.start();
          toggle.classList.add("hidden");
          if (stop) stop.classList.remove("hidden");
          if (status) status.textContent = "Идёт запись…";
        })
        .catch(function () {
          fail("Нет доступа к микрофону. Разрешите запись в настройках браузера или прикрепите файл.");
        });
    });

    if (stop) {
      stop.addEventListener("click", function () {
        if (recorder && recorder.state !== "inactive") recorder.stop();
        else reset();
      });
    }
  }

  /* ── Страница проверки: пресеты баллов, шаблоны, горячие клавиши ────────── */
  function reviewPage() {
    var page = document.querySelector("[data-review]");
    if (!page) return;
    var form = page.querySelector("[data-review-form]");
    if (!form) return;

    var grade = form.querySelector("#id_grade");
    var comment = form.querySelector("#id_comment");
    var decision = form.querySelector("#id_decision");
    var snippetInput = form.querySelector("#id_snippet_used");
    var maxPoints = parseInt(page.getAttribute("data-max-points"), 10) || 0;
    var hotkeys = page.getAttribute("data-hotkeys") !== "0";

    Array.prototype.forEach.call(
      page.querySelectorAll("[data-grade-preset]"),
      function (button) {
        button.addEventListener("click", function () {
          if (!grade || !maxPoints) return;
          var percent = parseInt(button.getAttribute("data-grade-preset"), 10) || 0;
          grade.value = String(Math.round((maxPoints * percent) / 100));
          grade.dispatchEvent(new Event("input", { bubbles: true }));
          grade.focus();
        });
      }
    );

    Array.prototype.forEach.call(page.querySelectorAll("[data-snippet]"), function (button) {
      button.addEventListener("click", function () {
        if (!comment) return;
        var text = button.getAttribute("data-snippet") || "";
        var current = comment.value.trim();
        comment.value = current ? current + "\n\n" + text : text;
        comment.dispatchEvent(new Event("input", { bubbles: true }));
        if (snippetInput) snippetInput.value = button.getAttribute("data-snippet-id") || "";
        comment.focus();
        comment.setSelectionRange(comment.value.length, comment.value.length);
      });
    });

    if (!hotkeys) return;

    document.addEventListener("keydown", function (event) {
      if (event.metaKey || event.altKey) return;
      if (event.ctrlKey) {
        if (event.key === "Enter") {
          event.preventDefault();
          submitWithNext();
        }
        return;
      }
      if (isTypingTarget(event.target)) return;

      var key = event.key.toLowerCase();
      if (key === "j") {
        var next = page.querySelector("[data-nav-next]");
        if (next) {
          event.preventDefault();
          next.click();
        }
      } else if (key === "k") {
        var prev = page.querySelector("[data-nav-prev]");
        if (prev) {
          event.preventDefault();
          prev.click();
        }
      } else if (key === "g" && grade) {
        event.preventDefault();
        grade.focus();
        grade.select();
      } else if (key === "c" && comment) {
        event.preventDefault();
        comment.focus();
      } else if (key === "r" && decision) {
        event.preventDefault();
        decision.focus();
      }
    });

    function submitWithNext() {
      var button = form.querySelector('button[name="next"]');
      if (button) {
        button.click();
      } else {
        form.submit();
      }
    }
  }

  /* ── Очередь: справка по клавишам ───────────────────────────────────────── */
  function queueHelp() {
    var panel = document.getElementById("hotkeys-help");
    if (!panel) return;
    document.addEventListener("keydown", function (event) {
      if (event.shiftKey && event.key === "?" && !isTypingTarget(event.target)) {
        event.preventDefault();
        panel.setAttribute("tabindex", "-1");
        panel.focus();
        panel.scrollIntoView({ block: "center", behavior: "smooth" });
      }
    });
  }

  ready(function () {
    autohideAlerts();
    confirmForms();
    draftAutosave();
    flashcards();
    audioRecorder();
    reviewPage();
    queueHelp();
  });
})();
