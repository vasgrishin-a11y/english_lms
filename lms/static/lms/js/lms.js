/* English LMS — прогрессивное улучшение интерфейса.
 * Работает без сборки, не требует htmx и не ломает сценарии без JavaScript:
 * автосохранение черновика, горячие клавиши проверки, карточки тренажёра,
 * запись аудио в браузере, вставка шаблонов комментариев, плитки упражнений.
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
      } catch (error) {}
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

  function dangerZones() {
    // Панели подтверждения удаления (<details class="danger-zone">): открыта
    // только одна, закрываются кликом мимо и по Escape и не уезжают за край экрана.
    function closeAll(except) {
      document.querySelectorAll("details.danger-zone[open]").forEach(function (d) {
        if (d !== except) d.removeAttribute("open");
      });
    }
    function fit(details) {
      var panel = details.querySelector(".danger-panel");
      if (!panel) return;
      panel.style.left = "";
      panel.style.right = "";
      var rect = panel.getBoundingClientRect();
      var margin = 8;
      var width = document.documentElement.clientWidth;
      if (rect.left < margin) {
        panel.style.right = "auto";
        panel.style.left = Math.round(margin - details.getBoundingClientRect().left) + "px";
      } else if (rect.right > width - margin) {
        panel.style.left = "auto";
        panel.style.right = Math.round(margin - (width - details.getBoundingClientRect().right)) + "px";
      }
    }
    document.addEventListener(
      "toggle",
      function (event) {
        var details = event.target;
        if (!details || !details.classList || !details.classList.contains("danger-zone")) return;
        if (!details.open) return;
        closeAll(details);
        fit(details);
      },
      true
    );
    document.addEventListener("click", function (event) {
      var inside = event.target.closest ? event.target.closest("details.danger-zone") : null;
      closeAll(inside);
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") closeAll(null);
    });
  }

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

  /* ── Запись ответа с микрофона ─────────────────────────────
   * Несколько рекордеров на странице (по одному на голосовой пункт).
   * Лимит преподавателя: обратный отсчёт, предупреждение за 10 секунд и
   * автоматическая остановка. Запись кодируется в WAV 16 кГц моно (~1,9 МБ
   * в минуту — 10 минут помещаются в лимит файла) и подставляется в поле
   * файла. Где браузер не даёт подставить файл (старый Safari), запись
   * добавляется в отправку формы напрямую.
   */
  var TARGET_RATE = 16000;

  function formatClock(seconds) {
    seconds = Math.max(0, Math.floor(seconds));
    var minutes = Math.floor(seconds / 60);
    var rest = seconds % 60;
    return minutes + ":" + (rest < 10 ? "0" : "") + rest;
  }

  function encodeWav(blob, callback) {
    var reader = new FileReader();
    reader.onload = function () {
      var Context = window.AudioContext || window.webkitAudioContext;
      if (!Context) {
        callback(blob, null);
        return;
      }
      var context = new Context();
      var done = function (buffer) {
        var channels = buffer.numberOfChannels;
        var ratio = buffer.sampleRate / TARGET_RATE;
        if (ratio < 1) ratio = 1;
        var rate = Math.round(buffer.sampleRate / ratio);
        var length = Math.floor(buffer.length / ratio);
        var data = [];
        for (var c = 0; c < channels; c += 1) data.push(buffer.getChannelData(c));
        var bytes = 44 + length * 2;
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
        view.setUint16(22, 1, true);
        view.setUint32(24, rate, true);
        view.setUint32(28, rate * 2, true);
        view.setUint16(32, 2, true);
        view.setUint16(34, 16, true);
        writeText(36, "data");
        view.setUint32(40, length * 2, true);
        var offset = 44;
        for (var i = 0; i < length; i += 1) {
          // Моно и понижение частоты: среднее по каналам и по окну исходных сэмплов.
          var from = Math.floor(i * ratio);
          var to = Math.min(buffer.length, Math.floor((i + 1) * ratio)) || from + 1;
          var sum = 0;
          var count = 0;
          for (var j = from; j < to; j += 1) {
            for (var k = 0; k < channels; k += 1) {
              sum += data[k][j];
              count += 1;
            }
          }
          var sample = Math.max(-1, Math.min(1, count ? sum / count : 0));
          view.setInt16(offset, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
          offset += 2;
        }
        if (context.close) context.close();
        callback(new Blob([view.buffer], { type: "audio/wav" }), buffer.duration);
      };
      var fail = function () {
        if (context.close) context.close();
        callback(blob, null);
      };
      var promise = context.decodeAudioData(reader.result, done, fail);
      if (promise && promise.catch) promise.catch(fail);
    };
    reader.readAsArrayBuffer(blob);
  }

  function setupRecorder(panel) {
    if (panel.dataset.recorderReady === "1") return;
    panel.dataset.recorderReady = "1";
    var toggle = panel.querySelector("[data-recorder-toggle]");
    var stop = panel.querySelector("[data-recorder-stop]");
    var status = panel.querySelector("[data-recorder-status]");
    var clock = panel.querySelector("[data-recorder-clock]");
    var meter = panel.querySelector("[data-recorder-meter]");
    var dot = panel.querySelector("[data-recorder-dot]");
    var take = panel.querySelector("[data-recorder-take]");
    var preview = panel.querySelector("[data-recorder-preview]");
    var discard = panel.querySelector("[data-recorder-discard]");
    var errorBox = panel.querySelector("[data-recorder-error]");
    var input = document.getElementById(panel.getAttribute("data-input") || "");
    var limit = parseInt(panel.getAttribute("data-limit"), 10) || 0;
    var actionLabel = panel.getAttribute("data-recorder-action") || "Принять или отправить";
    var form = panel.closest("form");
    if (!toggle) return;

    var recorder = null;
    var chunks = [];
    var stream = null;
    var timer = null;
    var startedAt = 0;
    var previewUrl = "";
    var syncingInput = false;

    function say(text) {
      if (status) status.textContent = text;
    }

    function showError(message) {
      if (errorBox) errorBox.textContent = message || "";
    }

    function tick() {
      var elapsed = (Date.now() - startedAt) / 1000;
      if (clock) clock.textContent = formatClock(elapsed) + (limit ? " / " + formatClock(limit) : "");
      if (limit) {
        var left = limit - elapsed;
        if (meter) meter.style.width = Math.min(100, (100 * elapsed) / limit) + "%";
        panel.classList.toggle("is-ending", left <= 10);
        if (left <= 0) {
          say("Время вышло — запись остановлена автоматически.");
          finish();
        }
      }
    }

    function release() {
      if (timer) window.clearInterval(timer);
      timer = null;
      if (stream) {
        stream.getTracks().forEach(function (track) {
          track.stop();
        });
      }
      stream = null;
      panel.classList.remove("is-recording", "is-ending");
      toggle.classList.remove("hidden");
      if (stop) stop.classList.add("hidden");
      if (dot) dot.classList.add("hidden");
    }

    function finish() {
      if (recorder && recorder.state !== "inactive") {
        recorder.stop();
      } else {
        release();
      }
    }

    function clearTake() {
      panel._recording = null;
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      previewUrl = "";
      if (preview) preview.removeAttribute("src");
      if (take) take.classList.add("hidden");
      if (input) {
        try {
          input.value = "";
        } catch (error) {}
        input.dispatchEvent(new Event("change", { bubbles: true }));
      }
      if (clock) clock.textContent = "0:00" + (limit ? " / " + formatClock(limit) : "");
      if (meter) meter.style.width = "0%";
      toggle.querySelector("span").textContent = "Начать запись";
    }

    function attach(blob, seconds) {
      var extension = blob.type === "audio/wav" ? ".wav" : ".webm";
      var name =
        "otvet-" + new Date().toISOString().slice(0, 19).replace(/[:T]/g, "") + extension;
      var file = new File([blob], name, { type: blob.type || "audio/wav" });
      panel._recording = file;
      if (input) {
        try {
          var transfer = new DataTransfer();
          transfer.items.add(file);
          syncingInput = true;
          input.files = transfer.files;
          input.dispatchEvent(new Event("change", { bubbles: true }));
          syncingInput = false;
        } catch (error) {
          syncingInput = false;
          // Старый Safari: файл уйдёт в отправку формы напрямую (см. submit ниже).
        }
      }
      previewUrl = URL.createObjectURL(blob);
      if (preview) preview.src = previewUrl;
      if (take) take.classList.remove("hidden");
      toggle.querySelector("span").textContent = "Записать заново";
      var length = seconds ? " (" + formatClock(seconds) + ")" : "";
      say("Запись готова" + length + ". Прослушайте её и нажмите «" + actionLabel + "».");
      showError("");
    }

    toggle.addEventListener("click", function () {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.MediaRecorder) {
        showError(
          window.isSecureContext === false
            ? "Микрофон доступен только по защищённому соединению (HTTPS). Загрузите аудиофайл."
            : "Браузер не поддерживает запись звука. Загрузите готовый аудиофайл."
        );
        return;
      }
      clearTake();
      showError("");
      say("Запрашиваем доступ к микрофону…");
      navigator.mediaDevices
        .getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } })
        .then(function (mediaStream) {
          stream = mediaStream;
          chunks = [];
          var mime = "";
          ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg"].some(function (type) {
            if (MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(type)) {
              mime = type;
              return true;
            }
            return false;
          });
          recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
          recorder.ondataavailable = function (event) {
            if (event.data && event.data.size) chunks.push(event.data);
          };
          recorder.onstop = function () {
            var raw = new Blob(chunks, { type: recorder.mimeType || mime || "audio/webm" });
            release();
            say("Обрабатываем запись…");
            encodeWav(raw, attach);
          };
          recorder.start(250);
          startedAt = Date.now();
          timer = window.setInterval(tick, 200);
          panel.classList.add("is-recording");
          toggle.classList.add("hidden");
          if (stop) {
            stop.classList.remove("hidden");
            stop.focus();
          }
          if (dot) dot.classList.remove("hidden");
          say(limit ? "Идёт запись. Лимит — " + formatClock(limit) + "." : "Идёт запись…");
        })
        .catch(function (error) {
          release();
          var denied = error && (error.name === "NotAllowedError" || error.name === "SecurityError");
          showError(
            denied
              ? "Нет доступа к микрофону. Разрешите его в настройках браузера (значок замка в адресной строке) или загрузите файл."
              : "Микрофон не найден или занят другим приложением. Подключите микрофон или загрузите файл."
          );
          say("Запись недоступна.");
        });
    });

    if (stop) stop.addEventListener("click", finish);
    if (discard) {
      discard.addEventListener("click", function () {
        clearTake();
        say("Запись удалена. Можно записать новую.");
        toggle.focus();
      });
    }
    if (input) {
      input.addEventListener("change", function () {
        // Событие, которое мы вызвали сами после записи, не должно скрывать превью.
        if (syncingInput) return;
        // Выбран файл вручную — он заменяет запись с микрофона.
        if (input.files && input.files[0]) {
          panel._recording = null;
          if (take) take.classList.add("hidden");
        }
      });
    }

    if (form && !form.dataset.recorderSubmit) {
      form.dataset.recorderSubmit = "1";
      form.addEventListener("submit", function (event) {
        var panels = form.querySelectorAll("[data-recorder]");
        for (var i = 0; i < panels.length; i += 1) {
          if (panels[i].classList.contains("is-recording")) {
            event.preventDefault();
            event.stopImmediatePropagation();
            showError("Сначала остановите запись.");
            return;
          }
        }
        if (form.hasAttribute("hx-post")) return; // htmx: см. htmx:configRequest
        var missing = pendingRecordings(form);
        if (!missing.length) return;
        // Поле файла не приняло запись — отправляем форму сами.
        event.preventDefault();
        var data = new FormData(form);
        missing.forEach(function (item) {
          data.set(item.name, item.file, item.file.name);
        });
        fetch(form.action || window.location.href, {
          method: "POST",
          body: data,
          credentials: "same-origin",
          headers: { "X-CSRFToken": csrfToken() },
        }).then(function (response) {
          window.location.href = response.url || window.location.href;
        });
      });
    }
  }

  function pendingRecordings(form) {
    var result = [];
    Array.prototype.forEach.call(form.querySelectorAll("[data-recorder]"), function (panel) {
      var input = document.getElementById(panel.getAttribute("data-input") || "");
      if (panel._recording && input && !(input.files && input.files.length)) {
        result.push({ name: input.name, file: panel._recording });
      }
    });
    return result;
  }

  function audioRecorder(root) {
    var scope = root && root.querySelectorAll ? root : document;
    Array.prototype.forEach.call(scope.querySelectorAll("[data-recorder]"), setupRecorder);
  }

  /* ── Пошаговая проверка пунктов ────────────────────────────
   * После «Принять» htmx заменяет пункт ответом сервера. Здесь — фокус на
   * результате (важно для клавиатуры и экранного диктора), запрет двойной
   * отправки и повторная инициализация плиток и рекордеров во фрагменте.
   */
  function quizItems() {
    document.body.addEventListener("htmx:configRequest", function (event) {
      var form = event.detail && event.detail.elt;
      if (!form || !form.querySelectorAll || !event.detail.formData) return;
      if (form.querySelector("[data-recorder].is-recording")) {
        event.preventDefault();
        var box = form.querySelector("[data-recorder-error]");
        if (box) box.textContent = "Сначала остановите запись.";
        return;
      }
      pendingRecordings(form).forEach(function (item) {
        event.detail.formData.set(item.name, item.file, item.file.name);
      });
    });
    document.body.addEventListener("htmx:afterSwap", function (event) {
      var target = event.detail && event.detail.target;
      if (!target) return;
      var item = document.getElementById(target.id) || target;
      if (item && item.matches && item.matches("[data-item]")) {
        quizTiles(item);
        audioRecorder(item);
        var feedback = item.querySelector(".item-note");
        var field = item.querySelector("input:not([type=hidden]), textarea, select");
        var state = item.getAttribute("data-state");
        if (state === "correct" || state === "failed" || (state === "answered" && !field)) {
          var next = item.nextElementSibling;
          while (next && next.getAttribute("data-state") !== "new" && next.getAttribute("data-state") !== "open") {
            next = next.nextElementSibling;
          }
          if (feedback) feedback.setAttribute("tabindex", "-1");
          if (feedback) feedback.focus({ preventScroll: true });
          item.classList.add("is-flash");
          window.setTimeout(function () {
            item.classList.remove("is-flash");
          }, 900);
          if (next) {
            window.setTimeout(function () {
              next.scrollIntoView({ behavior: "smooth", block: "center" });
            }, 700);
          }
        } else if (field) {
          field.focus({ preventScroll: true });
          if (field.select && field.type === "text") field.select();
        }
      }
    });
  }

  /* ── Форма пункта: поля по типу ───────────────────────────
   * Лимит записи нужен только голосовому пункту, варианты ответа —
   * только пунктам с автопроверкой. Скрытые поля не очищаются: сервер
   * сам обнуляет лишнее, а преподаватель не теряет набранное при смене типа.
   */
  function questionKindForms(root) {
    var scope = root && root.querySelectorAll ? root : document;
    Array.prototype.forEach.call(scope.querySelectorAll("select[name=kind]"), function (select) {
      var form = select.form;
      if (!form || select.dataset.kindReady === "1") return;
      select.dataset.kindReady = "1";
      var wrap = function (name) {
        var field = form.querySelector("[name^='" + name + "']");
        return field ? field.closest(".form-field") : null;
      };
      var limit = wrap("recording_limit_seconds");
      var choices = wrap("choices_text");
      var update = function () {
        var kind = select.value;
        var manual = kind === "text" || kind === "voice";
        if (limit) limit.classList.toggle("hidden", kind !== "voice");
        if (choices) choices.classList.toggle("hidden", manual);
      };
      select.addEventListener("change", update);
      update();
    });
  }

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

  function readJsonScript(id) {
    var node = document.getElementById(id);
    if (!node) return null;
    try {
      return JSON.parse(node.textContent);
    } catch (error) {
      return null;
    }
  }

  function setFieldValue(form, name, value) {
    var field = form.elements[name];
    if (!field || value === undefined || value === null) return false;
    if (typeof field.length === "number" && field.type === undefined) {
      var wanted = Array.prototype.map.call(value, String);
      Array.prototype.forEach.call(field, function (input) {
        input.checked = wanted.indexOf(String(input.value)) > -1;
        input.dispatchEvent(new Event("change", { bubbles: true }));
      });
      return true;
    }
    field.value = value;
    field.dispatchEvent(new Event("input", { bubbles: true }));
    field.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }

  function templatePresets() {
    var form = document.querySelector("form[data-preset-form]");
    if (!form) return;
    var data =
      readJsonScript("assignment-presets") ||
      readJsonScript("block-suggestions") ||
      readJsonScript("topic-suggestions") ||
      readJsonScript("card-presets");
    if (!data || !data.length) return;
    var skillIds = readJsonScript("assignment-skill-ids") || {};
    var hint = document.querySelector("[data-preset-hint]");
    var ignored = ["id", "icon", "label", "tagline", "skills", "fields"];

    function findPreset(key) {
      var byIndex = data[parseInt(key, 10)];
      if (byIndex) return byIndex;
      for (var index = 0; index < data.length; index += 1) {
        if (data[index].id === key) return data[index];
      }
      return null;
    }

    function presetLabel(item) {
      return item.label || item.title || item.name || "шаблон";
    }

    function hasContent() {
      var names = ["title", "name", "description"];
      for (var index = 0; index < names.length; index += 1) {
        var field = form.elements[names[index]];
        if (field && field.value && field.value.trim()) return true;
      }
      return false;
    }

    function apply(item) {
      var fields = item.fields || item;
      Object.keys(fields).forEach(function (name) {
        if (ignored.indexOf(name) > -1) return;
        setFieldValue(form, name, fields[name]);
      });
      // У формы карточек есть скрытое поле preset_id: сервер создаст карточки
      // из шаблона сразу при сохранении. У остальных форм поля нет — no-op.
      if (item.id) setFieldValue(form, "preset_id", item.id);
      if (item.skills && item.skills.length) {
        var ids = item.skills
          .map(function (slug) {
            return String(skillIds[slug] || "");
          })
          .filter(Boolean);
        if (ids.length) setFieldValue(form, "skills", ids);
        var manual = form.querySelector("#id_skills_manual");
        if (manual) manual.value = "1";
      }
    }

    Array.prototype.forEach.call(document.querySelectorAll("[data-preset]"), function (button) {
      button.setAttribute("aria-pressed", "false");
      button.addEventListener("click", function () {
        var item = findPreset(button.getAttribute("data-preset"));
        if (!item) return;
        if (hasContent() && !window.confirm("Заменить введённые данные шаблоном?")) return;
        apply(item);
        Array.prototype.forEach.call(
          document.querySelectorAll("[data-preset]"),
          function (other) {
            other.setAttribute("aria-pressed", other === button ? "true" : "false");
          }
        );
        if (hint) {
          hint.textContent =
            "Подставлен шаблон «" + presetLabel(item) + "» — отредактируйте детали и сохраните.";
        }
        var focusTarget = form.elements.title || form.elements.name;
        if (focusTarget) {
          focusTarget.scrollIntoView({ block: "center", behavior: "smooth" });
          focusTarget.focus();
        }
      });
    });
  }

  function quizTiles(root) {
    var scope = root && root.querySelectorAll ? root : document;
    var containers = scope.querySelectorAll("[data-tiles]");
    if (!containers.length) return;

    Array.prototype.forEach.call(containers, function (container) {
      var targetId = container.getAttribute("data-target");
      var input = document.getElementById(targetId);
      if (!input) return;
      var type = container.getAttribute("data-tiles");
      var slotRow = document.querySelector('[data-slot="' + targetId + '"]');

      function syncOrder() {
        if (!slotRow) return;
        var ids = [];
        Array.prototype.forEach.call(slotRow.querySelectorAll("[data-chip-id]"), function (chip) {
          ids.push(chip.getAttribute("data-chip-id"));
        });
        input.value = ids.join(",");
      }

      function syncSpell() {
        var word = "";
        Array.prototype.forEach.call(container.querySelectorAll(".tile.is-used"), function (tile) {
          word += tile.getAttribute("data-letter") || "";
        });
        if (word) input.value = word;
      }

      function createChip(id, text) {
        var chip = document.createElement("span");
        chip.className = "slot-chip";
        chip.setAttribute("data-chip-id", id);
        chip.textContent = text + " ";
        var remove = document.createElement("button");
        remove.type = "button";
        remove.textContent = "×";
        remove.setAttribute("aria-label", "Убрать " + text);
        remove.addEventListener("click", function () {
          var usedTiles = container.querySelectorAll(".tile.is-used");
          for (var i = 0; i < usedTiles.length; i++) {
            if ((usedTiles[i].getAttribute("data-word") || "") === text && (usedTiles[i].getAttribute("data-id") || "") === id) {
              usedTiles[i].classList.remove("is-used");
              break;
            }
          }
          // fallback: match by text only
          if (!container.querySelector('.tile.is-used[data-word="' + text + '"]')) {
            var byText = container.querySelectorAll(".tile.is-used");
            for (var j = 0; j < byText.length; j++) {
              if ((byText[j].getAttribute("data-word") || "") === text) {
                byText[j].classList.remove("is-used");
                break;
              }
            }
          }
          chip.parentNode.removeChild(chip);
          syncOrder();
        });
        chip.appendChild(remove);
        return chip;
      }

      if (type === "order" && slotRow) {
        Array.prototype.forEach.call(container.querySelectorAll(".tile"), function (tile) {
          tile.addEventListener("click", function () {
            if (tile.classList.contains("is-used")) return;
            tile.classList.add("is-used");
            var word = tile.getAttribute("data-word") || tile.textContent.trim();
            var chipId = tile.getAttribute("data-id") || word;
            slotRow.appendChild(createChip(chipId, word));
            syncOrder();
          });
        });
      }

      if (type === "spell") {
        Array.prototype.forEach.call(container.querySelectorAll(".tile"), function (tile) {
          tile.classList.add("is-letter");
          tile.addEventListener("click", function () {
            if (tile.classList.contains("is-used")) {
              tile.classList.remove("is-used");
            } else {
              tile.classList.add("is-used");
            }
            syncSpell();
          });
        });

        input.addEventListener("input", function () {
          if (!input.value) {
            Array.prototype.forEach.call(container.querySelectorAll(".tile.is-used"), function (t) {
              t.classList.remove("is-used");
            });
          }
        });
      }
    });
  }

  function pad(value) {
    return value < 10 ? "0" + value : String(value);
  }

  function localStamp(date) {
    return (
      date.getFullYear() +
      "-" +
      pad(date.getMonth() + 1) +
      "-" +
      pad(date.getDate()) +
      "T" +
      pad(date.getHours()) +
      ":" +
      pad(date.getMinutes())
    );
  }

  function deadlineShortcuts() {
    var field = document.getElementById("id_deadline");
    if (!field) return;
    Array.prototype.forEach.call(
      document.querySelectorAll("[data-deadline-shift]"),
      function (button) {
        button.addEventListener("click", function () {
          var days = parseInt(button.getAttribute("data-deadline-shift"), 10) || 0;
          var deadline = new Date();
          deadline.setDate(deadline.getDate() + days);
          deadline.setHours(23, 59, 0, 0);
          field.value = localStamp(deadline);
          field.dispatchEvent(new Event("input", { bubbles: true }));
          field.dispatchEvent(new Event("change", { bubbles: true }));
        });
      }
    );
  }

  function navToggle() {
    var button = document.getElementById("nav-toggle");
    if (!button) return;
    var root = document.documentElement;
    function sync() {
      var collapsed = root.getAttribute("data-nav") === "collapsed";
      var label = collapsed ? "Развернуть меню" : "Свернуть меню";
      button.setAttribute("aria-expanded", collapsed ? "false" : "true");
      button.setAttribute("aria-label", label);
      button.setAttribute("title", label);
    }
    sync();
    button.addEventListener("click", function () {
      var collapsed = root.getAttribute("data-nav") !== "collapsed";
      root.setAttribute("data-nav", collapsed ? "collapsed" : "expanded");
      try {
        if (collapsed) {
          window.localStorage.setItem("lms-nav-collapsed", "1");
        } else {
          window.localStorage.removeItem("lms-nav-collapsed");
        }
      } catch (error) {
        /* приватный режим: состояние просто не переживёт перезагрузку */
      }
      sync();
    });
  }

  function cardPresetFill() {
    var cards = readJsonScript("card-preset-cards");
    if (!cards) return;
    var hint = document.querySelector("[data-card-preset-hint]");
    Array.prototype.forEach.call(
      document.querySelectorAll("[data-card-preset]"),
      function (button) {
        button.addEventListener("click", function () {
          var text = cards[button.getAttribute("data-card-preset")];
          if (!text) return;
          // Форма массового импорта с префиксом bulk: имя поля bulk-cards_text.
          var area = document.querySelector(
            "textarea[name='bulk-cards_text'], textarea[name='cards_text']"
          );
          if (!area) return;
          if (
            area.value &&
            area.value.trim() &&
            !window.confirm("Заменить содержимое поля списком карточек из шаблона?")
          ) {
            return;
          }
          area.value = text;
          area.dispatchEvent(new Event("input", { bubbles: true }));
          area.scrollIntoView({ block: "center", behavior: "smooth" });
          area.focus();
          if (hint) {
            hint.textContent =
              "Список карточек подставлен из шаблона — нажмите «Добавить карточки».";
          }
        });
      }
    );
  }

  function presetLevelFilter() {
    Array.prototype.forEach.call(
      document.querySelectorAll("[data-level-filter]"),
      function (bar) {
        var scope = bar.closest("fieldset, section") || document;
        var chips = bar.querySelectorAll("[data-level-filter-value]");
        function apply(level) {
          Array.prototype.forEach.call(chips, function (chip) {
            chip.setAttribute(
              "aria-pressed",
              chip.getAttribute("data-level-filter-value") === level ? "true" : "false"
            );
          });
          Array.prototype.forEach.call(
            scope.querySelectorAll("[data-level]"),
            function (item) {
              item.hidden = !!level && item.getAttribute("data-level") !== level;
            }
          );
          Array.prototype.forEach.call(
            scope.querySelectorAll("[data-preset-group]"),
            function (group) {
              group.hidden = group.querySelectorAll("[data-level]:not([hidden])").length === 0;
            }
          );
        }
        Array.prototype.forEach.call(chips, function (chip) {
          chip.addEventListener("click", function () {
            apply(chip.getAttribute("data-level-filter-value") || "");
          });
        });
      }
    );
  }

  function assignmentTypeForm() {
    var form = document.querySelector("[data-assignment-form]");
    if (!form) return;
    var typeField = form.elements.assignment_type;
    var skillsField = form.elements.skills;
    var manual = form.querySelector("#id_skills_manual");
    var byType = readJsonScript("assignment-skills-by-type") || {};
    var panels = document.querySelectorAll("[data-show-types]");

    function selectedType() {
      if (!typeField) return "";
      if (typeof typeField.value === "string") return typeField.value;
      for (var i = 0; i < typeField.length; i += 1) {
        if (typeField[i].checked) return typeField[i].value;
      }
      return "";
    }

    function applySkills(type) {
      if (!skillsField || (manual && manual.value === "1")) return;
      var ids = (byType[type] || []).map(String);
      setFieldValue(form, "skills", ids);
    }

    function syncPanels(type) {
      Array.prototype.forEach.call(panels, function (panel) {
        var allowed = (panel.getAttribute("data-show-types") || "").split(/\s+/);
        var show = !type || allowed.indexOf(type) > -1;
        panel.hidden = !show;
      });
    }

    function onTypeChange() {
      var type = selectedType();
      applySkills(type);
      syncPanels(type);
    }

    if (typeField) {
      if (typeof typeField.length === "number" && typeField.type === undefined) {
        Array.prototype.forEach.call(typeField, function (input) {
          input.addEventListener("change", onTypeChange);
        });
      } else {
        typeField.addEventListener("change", onTypeChange);
      }
    }
    if (skillsField) {
      var boxes =
        typeof skillsField.length === "number" && skillsField.type === undefined
          ? skillsField
          : [skillsField];
      Array.prototype.forEach.call(boxes, function (input) {
        input.addEventListener("change", function () {
          if (manual) manual.value = "1";
        });
      });
    }
    syncPanels(selectedType());
  }

  function typeahead() {
    var inputs = document.querySelectorAll("[data-suggest]");
    if (!inputs.length) return;

    Array.prototype.forEach.call(inputs, function (input) {
      var url = input.getAttribute("data-suggest");
      var scope = input.getAttribute("data-suggest-scope") || "";
      if (!url) return;
      var host = input.closest(".search-pill, .search, form") || input.parentNode;
      if (host && window.getComputedStyle(host).position === "static") {
        host.style.position = "relative";
      }
      var list = document.createElement("ul");
      list.className = "suggest-list";
      list.hidden = true;
      list.setAttribute("role", "listbox");
      if (host) host.appendChild(list);
      var timer = null;
      var items = [];
      var active = -1;

      function close() {
        list.hidden = true;
        list.innerHTML = "";
        items = [];
        active = -1;
      }

      function highlight() {
        var buttons = list.querySelectorAll(".suggest-item");
        Array.prototype.forEach.call(buttons, function (button, index) {
          button.classList.toggle("is-active", index === active);
        });
      }

      function go(index) {
        var item = items[index];
        if (!item || !item.url) return;
        window.location.href = item.url;
      }

      function render(next) {
        items = next || [];
        active = items.length ? 0 : -1;
        list.innerHTML = "";
        if (!items.length) {
          close();
          return;
        }
        items.forEach(function (item, index) {
          var li = document.createElement("li");
          var button = document.createElement("button");
          button.type = "button";
          button.className = "suggest-item";
          button.setAttribute("role", "option");
          var label = document.createElement("span");
          label.className = "suggest-item-label";
          label.textContent = item.label || "";
          button.appendChild(label);
          if (item.hint) {
            var hint = document.createElement("span");
            hint.className = "suggest-item-hint";
            hint.textContent = item.hint;
            button.appendChild(hint);
          }
          button.addEventListener("mousedown", function (event) {
            event.preventDefault();
            go(index);
          });
          li.appendChild(button);
          list.appendChild(li);
        });
        list.hidden = false;
        highlight();
      }

      function lookup() {
        var query = (input.value || "").trim();
        if (query.length < 1) {
          close();
          return;
        }
        var target = url + (url.indexOf("?") > -1 ? "&" : "?") + "q=" + encodeURIComponent(query);
        if (scope) target += "&scope=" + encodeURIComponent(scope);
        fetch(target, { credentials: "same-origin", headers: { Accept: "application/json" } })
          .then(function (response) {
            if (!response.ok) throw new Error("HTTP " + response.status);
            return response.json();
          })
          .then(function (payload) {
            if ((input.value || "").trim() !== query) return;
            render(payload.items || []);
          })
          .catch(function () {
            close();
          });
      }

      input.setAttribute("aria-autocomplete", "list");
      input.addEventListener("input", function () {
        window.clearTimeout(timer);
        timer = window.setTimeout(lookup, 160);
      });
      input.addEventListener("keydown", function (event) {
        if (list.hidden || !items.length) return;
        if (event.key === "ArrowDown") {
          event.preventDefault();
          active = (active + 1) % items.length;
          highlight();
        } else if (event.key === "ArrowUp") {
          event.preventDefault();
          active = (active - 1 + items.length) % items.length;
          highlight();
        } else if (event.key === "Enter" && active > -1) {
          event.preventDefault();
          go(active);
        } else if (event.key === "Escape") {
          close();
        }
      });
      input.addEventListener("blur", function () {
        window.setTimeout(close, 120);
      });
    });
  }

  var UPLOAD_ICON =
    '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" ' +
    'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M12 16V4"/><path d="m7 9 5-5 5 5"/>' +
    '<path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/></svg>';

  function humanSize(bytes) {
    if (!bytes) return "";
    var units = ["Б", "КБ", "МБ", "ГБ"];
    var index = 0;
    var value = bytes;
    while (value >= 1024 && index < units.length - 1) {
      value = value / 1024;
      index += 1;
    }
    var rounded = index === 0 ? String(value) : value.toFixed(value < 10 ? 1 : 0);
    return rounded.replace(".", ",") + " " + units[index];
  }

  function acceptLabel(accept) {
    var parts = (accept || "")
      .split(",")
      .map(function (item) {
        return item.trim();
      })
      .filter(Boolean);
    var extensions = parts
      .filter(function (item) {
        return item.charAt(0) === ".";
      })
      .map(function (item) {
        return item.slice(1).toUpperCase();
      });
    if (!extensions.length) return "";
    if (extensions.length > 6) return extensions.slice(0, 6).join(", ") + " и другие";
    return extensions.join(", ");
  }

  function uploadDropzones() {
    var inputs = document.querySelectorAll('input[type="file"][data-dropzone]');
    Array.prototype.forEach.call(inputs, function (input) {
      if (input.dataset.dropzoneReady === "1" || input.disabled) return;
      input.dataset.dropzoneReady = "1";
      if (!input.id) input.id = "dropzone-" + Math.floor(Math.random() * 1000000);

      var maxMb = parseFloat(input.getAttribute("data-max-mb")) || 0;
      var maxBytes = maxMb * 1024 * 1024;
      var accept = input.getAttribute("accept") || "";
      var hint =
        input.getAttribute("data-dropzone-hint") || "Перетащите файл сюда или выберите на диске";
      var isMultiple = input.hasAttribute("multiple");
      var limits = [];
      var extensions = acceptLabel(accept);
      if (extensions) limits.push(extensions);
      if (maxMb) limits.push("до " + maxMb + " МБ");
      if (isMultiple) limits.push("можно несколько");

      var zone = document.createElement("div");
      zone.className = "dropzone";
      zone.innerHTML =
        '<span class="dropzone-icon" aria-hidden="true">' +
        UPLOAD_ICON +
        "</span>" +
        '<span class="dropzone-body">' +
        '<span class="dropzone-title">' +
        hint +
        "</span>" +
        '<span class="dropzone-hint">' +
        (limits.join(" · ") || "Любой поддерживаемый формат") +
        "</span>" +
        '<span class="dropzone-file" data-dropzone-name hidden></span>' +
        '<span class="dropzone-error" data-dropzone-error role="alert" hidden></span>' +
        "</span>" +
        '<button type="button" class="btn btn-secondary btn-sm" data-dropzone-pick>' + (isMultiple ? "Выбрать файлы" : "Выбрать файл") + '</button>' +
        '<button type="button" class="btn btn-ghost btn-sm" data-dropzone-clear hidden>' + (isMultiple ? "Убрать все" : "Убрать файл") + '</button>';

      input.parentNode.insertBefore(zone, input);
      input.classList.add("dropzone-native");
      zone.appendChild(input);

      var nameEl = zone.querySelector("[data-dropzone-name]");
      var errorEl = zone.querySelector("[data-dropzone-error]");
      var pick = zone.querySelector("[data-dropzone-pick]");
      var clearBtn = zone.querySelector("[data-dropzone-clear]");

      function showError(message) {
        if (!errorEl) return;
        errorEl.textContent = message || "";
        errorEl.hidden = !message;
      }

      function render() {
        var files = input.files;
        if (!files || !files.length) {
          nameEl.hidden = true;
          nameEl.textContent = "";
          clearBtn.hidden = true;
          zone.classList.remove("is-filled");
          return;
        }
        nameEl.hidden = false;
        nameEl.textContent = "";
        // Если много файлов — список
        if (files.length > 1) {
          var ul = document.createElement("div");
          ul.style.display = "flex";
          ul.style.flexDirection = "column";
          ul.style.gap = "2px";
          for (var i = 0; i < files.length; i++) {
            var row = document.createElement("span");
            row.style.display = "flex";
            row.style.gap = "8px";
            row.style.alignItems = "center";
            var t = document.createElement("span");
            t.textContent = files[i].name;
            var s = document.createElement("span");
            s.className = "dropzone-size";
            s.textContent = humanSize(files[i].size);
            row.appendChild(t);
            row.appendChild(s);
            ul.appendChild(row);
          }
          nameEl.appendChild(ul);
        } else {
          var file = files[0];
          var title = document.createElement("span");
          title.textContent = file.name;
          var size = document.createElement("span");
          size.className = "dropzone-size";
          size.textContent = humanSize(file.size);
          nameEl.appendChild(title);
          nameEl.appendChild(size);
        }
        clearBtn.hidden = false;
        zone.classList.add("is-filled");
        // Снимаем галку clear, если файлы выбраны заново
        var clearBox = (input.form || document).querySelector('input[name="' + input.name + '-clear"]');
        if (clearBox && files.length) clearBox.checked = false;
      }

      function check(file) {
        if (!file) return true;
        if (maxBytes && file.size > maxBytes) {
          showError("Файл больше " + maxMb + " МБ: выберите файл поменьше.");
          return false;
        }
        var allowed = accept
          .split(",")
          .map(function (item) {
            return item.trim().toLowerCase();
          })
          .filter(function (item) {
            return item.charAt(0) === ".";
          });
        var dot = file.name.lastIndexOf(".");
        var extension = dot > -1 ? file.name.slice(dot).toLowerCase() : "";
        if (allowed.length && allowed.indexOf(extension) === -1) {
          showError("Формат " + (extension || "без расширения") + " здесь не принимается.");
          return false;
        }
        showError("");
        return true;
      }

      function acceptFiles(fileList) {
        if (!fileList || !fileList.length) return;
        // Проверка всех файлов
        for (var k = 0; k < fileList.length; k++) {
          if (!check(fileList[k])) {
            if (!isMultiple) {
              input.value = "";
              render();
            }
            return;
          }
        }
        try {
          var transfer = new DataTransfer();
          if (isMultiple) {
            // Добавляем к уже выбранным
            if (input.files) {
              for (var j = 0; j < input.files.length; j++) transfer.items.add(input.files[j]);
            }
            for (var idx = 0; idx < fileList.length; idx++) transfer.items.add(fileList[idx]);
          } else {
            transfer.items.add(fileList[0]);
          }
          input.files = transfer.files;
        } catch (error) {
          // Fallback без DataTransfer — только одиночный файл
        }
        render();
        input.dispatchEvent(new Event("change", { bubbles: true }));
      }

      if (pick) {
        pick.addEventListener("click", function (event) {
          event.preventDefault();
          input.click();
        });
      }
      zone.addEventListener("click", function (event) {
        if (event.target.closest("button, input, label")) return;
        input.click();
      });
      ["dragenter", "dragover"].forEach(function (evName) {
        zone.addEventListener(evName, function (event) {
          event.preventDefault();
          zone.classList.add("is-dragging");
        });
      });
      ["dragleave", "dragend", "drop"].forEach(function (evName) {
        zone.addEventListener(evName, function (event) {
          event.preventDefault();
          if (evName !== "drop" || !event.dataTransfer || !event.dataTransfer.files.length) {
            zone.classList.remove("is-dragging");
          }
        });
      });
      zone.addEventListener("drop", function (event) {
        zone.classList.remove("is-dragging");
        acceptFiles(event.dataTransfer ? event.dataTransfer.files : null);
      });
      // Поддержка Ctrl+V прямо в зоне
      zone.addEventListener("paste", function (event) {
        var items = (event.clipboardData || window.clipboardData).items;
        if (!items) return;
        var files = [];
        for (var i = 0; i < items.length; i++) {
          if (items[i].kind === "file") {
            var f = items[i].getAsFile();
            if (f) files.push(f);
          }
        }
        if (files.length) {
          event.preventDefault();
          acceptFiles(files);
        }
      });
      if (clearBtn) {
        clearBtn.addEventListener("click", function (event) {
          event.preventDefault();
          event.stopPropagation();
          input.value = "";
          // Removing a newly selected replacement must not delete the saved file.
          // Saved files are removed only via the explicit deletion checkbox.
          // Для multiple просто очищаем DataTransfer
          try {
            var dt = new DataTransfer();
            input.files = dt.files;
          } catch (e) {}
          showError("");
          render();
          input.dispatchEvent(new Event("change", { bubbles: true }));
        });
      }
      input.addEventListener("change", function () {
        if (input.files) {
          for (var i = 0; i < input.files.length; i++) {
            if (!check(input.files[i])) {
              if (!isMultiple) {
                input.value = "";
                try { var d = new DataTransfer(); input.files = d.files; } catch(e){}
              }
              break;
            }
          }
        }
        render();
      });
      // Если в шаблоне уже есть checkbox clear, слушаем его
      var extClear = (input.form || document).querySelector('input[name="' + input.name + '-clear"]');
      if (extClear) {
        extClear.addEventListener("change", function () {
          if (extClear.checked) {
            input.value = "";
            input.dispatchEvent(new Event("change", { bubbles: true }));
            zone.classList.add("is-cleared");
          } else {
            zone.classList.remove("is-cleared");
          }
        });
      }
      render();
    });
  }

  function descriptionEditors() {
    var blocks = document.querySelectorAll("[data-editor-block]");
    Array.prototype.forEach.call(blocks, function (block) {
      var textarea = block.querySelector("textarea");
      var expand = block.querySelector("[data-editor-expand]");
      var restore = block.querySelector("[data-editor-restore]");
      var note = block.querySelector("[data-editor-note]");
      if (!textarea || !expand) return;
      var label = expand.querySelector("[data-editor-expand-label]");

      function apply(open) {
        block.classList.toggle("is-expanded", open);
        expand.setAttribute("aria-expanded", open ? "true" : "false");
        if (label) label.textContent = open ? "Свернуть" : "Развернуть";
        if (restore) restore.hidden = !open;
        if (note) note.hidden = !open;
      }

      expand.addEventListener("click", function () {
        var open = !block.classList.contains("is-expanded");
        apply(open);
        if (open) {
          textarea.focus();
          textarea.setSelectionRange(textarea.value.length, textarea.value.length);
        }
      });
      if (restore) {
        restore.addEventListener("click", function () {
          apply(false);
          textarea.focus();
        });
      }
      textarea.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && block.classList.contains("is-expanded")) {
          event.stopPropagation();
          apply(false);
        }
      });
    });
  }

  /* ── Карта курса: быстрые действия на доске ─────────────
   * Переименование, статус и порядок перетаскиванием работают без
   * перезагрузки. Без JS остаются обычные формы: кнопки ↑/↓ для порядка,
   * кнопка статуса и полная форма задания.
   */
  // Раскрыть скрытую группу под курсором при перетаскивании (ставит curriculumCollapse).
  var curriculumRevealGroups = null;

  /* ── Карта курса: свёртка «классы → главы → темы» ────────────
   * Три глубины: 1 — только классы, 2 — классы и главы (по умолчанию),
   * 3 — классы, главы и темы. Общий переключатель задаёт глубину всему
   * курсу, такой же мини-переключатель — каждому классу; заголовок класса
   * и заголовок главы складываются и раскрываются по клику вручную.
   */
  function curriculumCollapse() {
    var board = document.querySelector("[data-curriculum-board]");
    if (!board) return;
    // По умолчанию видны классы и главы; при поиске — всё, чтобы совпадения были видны.
    var DEFAULT_LEVEL = parseInt(board.getAttribute("data-default-depth"), 10) || 2;

    function isBlock(node) {
      return !!node && !!node.getAttribute && node.getAttribute("data-block-drop") !== null;
    }

    function blocksIn(scope) {
      return isBlock(scope) ? [scope] : Array.prototype.slice.call(scope.querySelectorAll("[data-block-drop]"));
    }

    function groupsIn(scope, kind) {
      return Array.prototype.slice.call(scope.querySelectorAll('[data-curriculum-group="' + kind + '"]'));
    }

    function toggleFor(id) {
      if (!id) return null;
      var list = document.querySelectorAll("[data-collapse-toggle]");
      for (var i = 0; i < list.length; i += 1) {
        if (list[i].getAttribute("aria-controls") === id) return list[i];
      }
      return null;
    }

    function isOpen(group) {
      return !!group && !group.hasAttribute("hidden");
    }

    function setGroup(group, open) {
      if (!group) return;
      if (open) group.removeAttribute("hidden");
      else group.setAttribute("hidden", "");
      var toggle = toggleFor(group.id);
      if (toggle) toggle.setAttribute("aria-expanded", open ? "true" : "false");
      // Кнопки «Глава/Тема» внизу класса живут и прячутся вместе с главами.
      if (group.getAttribute("data-curriculum-group") === "chapters") {
        var section = group.closest("[data-block-drop]");
        var footer = section && section.querySelector("[data-block-footer]");
        if (footer) {
          if (open) footer.removeAttribute("hidden");
          else footer.setAttribute("hidden", "");
        }
      }
    }

    // 1 — свёрнут весь класс, 2 — видны главы, 3 — видны и темы,
    // 0 — смешанное состояние (главы раскрыты выборочно).
    function levelOf(section) {
      if (!isOpen(groupsIn(section, "chapters")[0])) return 1;
      var topics = groupsIn(section, "topics");
      if (!topics.length) return 2;
      var opened = 0;
      for (var i = 0; i < topics.length; i += 1) {
        if (isOpen(topics[i])) opened += 1;
      }
      if (opened === 0) return 2;
      return opened === topics.length ? 3 : 0;
    }

    function boardLevel() {
      var sections = blocksIn(board);
      if (!sections.length) return DEFAULT_LEVEL;
      var level = levelOf(sections[0]);
      for (var i = 1; i < sections.length; i += 1) {
        if (levelOf(sections[i]) !== level) return 0;
      }
      return level;
    }

    function markControl(control, level) {
      Array.prototype.forEach.call(control.querySelectorAll("[data-depth-option]"), function (option) {
        var value = parseInt(option.getAttribute("data-depth-level"), 10);
        option.setAttribute("aria-pressed", value === level ? "true" : "false");
      });
    }

    function refreshControls() {
      Array.prototype.forEach.call(document.querySelectorAll("[data-depth-control]"), function (control) {
        if (control.getAttribute("data-depth-control") === "all") {
          markControl(control, boardLevel());
          return;
        }
        var section = control.closest ? control.closest("[data-block-drop]") : null;
        if (section) markControl(control, levelOf(section));
      });
    }

    function applyLevel(scope, level) {
      blocksIn(scope).forEach(function (section) {
        setGroup(groupsIn(section, "chapters")[0], level >= 2);
        groupsIn(section, "topics").forEach(function (group) {
          setGroup(group, level >= 3);
        });
      });
      refreshControls();
    }

    function revealChaptersOf(node) {
      var section = node && node.closest ? node.closest("[data-block-drop]") : null;
      if (!section) return;
      var chapters = groupsIn(section, "chapters")[0];
      if (chapters && !isOpen(chapters)) {
        setGroup(chapters, true);
        refreshControls();
      }
    }

    // Тему тащат в свёрнутую главу — раскрываем её, чтобы место падения было видно.
    curriculumRevealGroups = function (node) {
      var chapter = node && node.closest ? node.closest("[data-chapter-panel]") : null;
      if (!chapter) return;
      var group = groupsIn(chapter, "topics")[0];
      if (group && !isOpen(group)) {
        setGroup(group, true);
        refreshControls();
      }
    };

    document.addEventListener("click", function (event) {
      var target = event.target.closest ? event.target : null;
      if (!target) return;
      var option = target.closest("[data-depth-option]");
      if (option) {
        var control = option.closest("[data-depth-control]");
        if (!control) return;
        event.preventDefault();
        var level = parseInt(option.getAttribute("data-depth-level"), 10) || DEFAULT_LEVEL;
        var section = control.closest("[data-block-drop]");
        if (control.getAttribute("data-depth-control") === "all" || !section) applyLevel(board, level);
        else applyLevel(section, level);
        return;
      }
      var toggle = target.closest("[data-collapse-toggle]");
      if (toggle) {
        var group = document.getElementById(toggle.getAttribute("aria-controls") || "");
        if (!group) return;
        event.preventDefault();
        setGroup(group, !isOpen(group));
        refreshControls();
        return;
      }
      // Карточка класса ведёт к классу: если он свёрнут — раскрываем.
      var jump = target.closest('a[href^="#block-"]');
      if (jump) revealChaptersOf(document.getElementById((jump.getAttribute("href") || "").slice(1)));
    });

    applyLevel(board, DEFAULT_LEVEL);
  }

  function curriculumBoard() {
    var boardEl = document.querySelector("[data-curriculum-board]");
    if (!boardEl) return;

    function postForm(url, params) {
      return fetch(url, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "X-CSRFToken": csrfToken(),
          "X-Requested-With": "XMLHttpRequest",
          "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
        },
        body: new URLSearchParams(params).toString()
      }).then(function (response) {
        return response.json().then(function (data) {
          if (!response.ok || !data || !data.ok) {
            throw new Error((data && data.error) || "Ошибка " + response.status);
          }
          return data;
        });
      });
    }

    // Статус «черновик ⟷ опубликовано» без перезагрузки.
    Array.prototype.forEach.call(boardEl.querySelectorAll("form[data-board-toggle]"), function (form) {
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        var button = form.querySelector('button[type="submit"]');
        if (button) button.disabled = true;
        postForm(form.action, new FormData(form))
          .then(function (data) {
            var published = !!data.published;
            var row = form.closest("[data-assignment-row]");
            var badge = row && row.querySelector("[data-status-badge]");
            if (badge) {
              badge.textContent = published ? "опубликовано" : "черновик";
              badge.classList.toggle("badge-checked", published);
              badge.classList.toggle("badge-draft", !published);
            }
            var input = form.querySelector('input[name="publish"]');
            if (input) input.value = published ? "0" : "1";
            var label = form.querySelector("[data-toggle-label]");
            var nextLabel = published
              ? form.getAttribute("data-label-unpublish")
              : form.getAttribute("data-label-publish");
            if (label && nextLabel) label.textContent = nextLabel;
            if (button && nextLabel) button.setAttribute("title", nextLabel);
          })
          .catch(function () {
            form.submit(); // резервный путь: обычная отправка с перезагрузкой
          })
          .finally(function () {
            if (button) button.disabled = false;
          });
      });
    });

    // Переименование по карандашу рядом с названием.
    Array.prototype.forEach.call(boardEl.querySelectorAll("[data-rename]"), function (button) {
      button.addEventListener("click", function () {
        var row = button.closest("[data-assignment-row]");
        var titleEl = row && row.querySelector("[data-title]");
        if (!titleEl || row.querySelector("[data-rename-input]")) return;
        var current = titleEl.textContent.trim();
        var input = document.createElement("input");
        input.type = "text";
        input.value = current;
        input.maxLength = 200;
        input.className = "inline-rename-input";
        input.setAttribute("data-rename-input", "1");
        input.setAttribute("aria-label", button.getAttribute("title") || "Новое название задания");
        titleEl.hidden = true;
        titleEl.parentNode.insertBefore(input, titleEl.nextSibling);
        input.focus();
        input.select();
        var finished = false;
        var finish = function (save) {
          if (finished) return;
          finished = true;
          var value = input.value.trim();
          input.remove();
          titleEl.hidden = false;
          if (!save || !value || value === current) return;
          var params = new FormData();
          params.set("title", value);
          postForm(button.getAttribute("data-rename-url"), params)
            .then(function (data) {
              titleEl.textContent = data.title || value;
            })
            .catch(function (error) {
              window.alert(error.message || "Не удалось переименовать задание");
            });
        };
        input.addEventListener("keydown", function (event) {
          if (event.key === "Enter") {
            event.preventDefault();
            finish(true);
          }
          if (event.key === "Escape") {
            event.preventDefault();
            finish(false);
          }
        });
        input.addEventListener("blur", function () {
          finish(true);
        });
      });
    });

    // Перетаскивание: задания — внутри темы и между темами; темы — внутри главы,
    // между главами класса (перенос) и между классами (копия); главы — внутри класса.
    var drag = null;

    function rowOf(node) {
      return node && node.closest ? node.closest("[data-assignment-row]") : null;
    }

    function panelOf(node) {
      return node && node.closest ? node.closest("[data-topic-panel]") : null;
    }

    function chapterOf(node) {
      return node && node.closest ? node.closest("[data-chapter-panel]") : null;
    }

    function blockOf(node) {
      return node && node.closest ? node.closest("[data-block-drop]") : null;
    }

    function topicsContainerOf(node) {
      return node && node.closest ? node.closest("[data-chapter-topics]") : null;
    }

    function clearMarkers() {
      Array.prototype.forEach.call(
        boardEl.querySelectorAll(".drop-above, .drop-below, .drop-block-active"),
        function (el) {
          el.classList.remove("drop-above", "drop-below", "drop-block-active");
        }
      );
    }

    function isBelowMiddle(event, el) {
      var rect = el.getBoundingClientRect();
      return event.clientY - rect.top >= rect.height / 2;
    }

    function topicTitle(el) {
      var link = el.querySelector("a");
      return link ? link.textContent.trim() : "тема";
    }

    Array.prototype.forEach.call(boardEl.querySelectorAll("[data-dnd-handle]"), function (handle) {
      handle.addEventListener("dragstart", function (event) {
        var kind = handle.getAttribute("data-dnd-handle");
        var el = kind === "assignment" ? rowOf(handle) : kind === "chapter" ? chapterOf(handle) : panelOf(handle);
        if (!el || !el.getAttribute("data-move-url")) {
          event.preventDefault();
          return;
        }
        drag = {
          kind: kind,
          el: el,
          blockId: handle.getAttribute("data-block-id") || el.getAttribute("data-block-id") || "",
          chapterId: handle.getAttribute("data-chapter-id") || el.getAttribute("data-chapter-id") || "",
          topicId: handle.getAttribute("data-topic-id") || el.getAttribute("data-topic-panel") || ""
        };
        el.classList.add("is-dragging");
        if (event.dataTransfer) {
          event.dataTransfer.effectAllowed = kind === "topic" ? "copyMove" : "move";
          try {
            event.dataTransfer.setData("text/plain", "move");
          } catch (ignore) {}
        }
      });
      handle.addEventListener("dragend", function () {
        clearMarkers();
        if (drag && drag.el) drag.el.classList.remove("is-dragging");
        drag = null;
      });
    });

    // Куда упадёт тема: панель-ориентир, контейнер главы, пустой класс.
    function topicTarget(event) {
      var targetPanel = panelOf(event.target);
      var container = topicsContainerOf(event.target);
      var chapter = chapterOf(event.target);
      var section = blockOf(event.target);
      if (targetPanel && targetPanel !== drag.el) {
        return {
          panel: targetPanel,
          container: targetPanel.parentNode,
          blockId: targetPanel.getAttribute("data-block-id"),
          chapterId: targetPanel.getAttribute("data-chapter-id"),
          before: isBelowMiddle(event, targetPanel) ? targetPanel.nextElementSibling : targetPanel
        };
      }
      if (container) {
        return {
          panel: null,
          container: container,
          blockId: container.getAttribute("data-block-topics"),
          chapterId: container.getAttribute("data-chapter-topics"),
          before: null
        };
      }
      if (chapter) {
        var list = chapter.querySelector("[data-chapter-topics]");
        return {
          panel: null,
          container: list,
          blockId: chapter.getAttribute("data-block-id"),
          chapterId: chapter.getAttribute("data-chapter-panel"),
          before: null
        };
      }
      if (section) {
        return {
          panel: null,
          container: null,
          blockId: section.getAttribute("data-block-id"),
          chapterId: "",
          before: null
        };
      }
      return null;
    }

    boardEl.addEventListener("dragover", function (event) {
      if (!drag) return;
      clearMarkers();
      if (drag.kind === "assignment") {
        var row = rowOf(event.target);
        var panel = panelOf(event.target);
        if ((!row && !panel) || row === drag.el) return;
        event.preventDefault();
        if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
        if (row) row.classList.add(isBelowMiddle(event, row) ? "drop-below" : "drop-above");
        else if (panel) panel.classList.add("drop-block-active");
        return;
      }
      if (drag.kind === "chapter") {
        var targetChapter = chapterOf(event.target);
        if (!targetChapter || targetChapter === drag.el) return;
        if (targetChapter.getAttribute("data-block-id") !== drag.blockId) return;
        event.preventDefault();
        if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
        targetChapter.classList.add(isBelowMiddle(event, targetChapter) ? "drop-below" : "drop-above");
        return;
      }
      // Свёрнутая глава под курсором раскрывается: иначе тема упала бы в невидимый список.
      if (curriculumRevealGroups) curriculumRevealGroups(event.target);
      var target = topicTarget(event);
      if (!target) return;
      event.preventDefault();
      var crossBlock = target.blockId && drag.blockId && target.blockId !== drag.blockId;
      if (event.dataTransfer) event.dataTransfer.dropEffect = crossBlock ? "copy" : "move";
      if (target.panel) {
        target.panel.classList.add(isBelowMiddle(event, target.panel) ? "drop-below" : "drop-above");
      } else if (target.container) {
        target.container.classList.add("drop-block-active");
      } else {
        var section = blockOf(event.target);
        if (section) section.classList.add("drop-block-active");
      }
    });

    boardEl.addEventListener("drop", function (event) {
      if (!drag) return;
      clearMarkers();
      if (drag.kind === "assignment") {
        var row = rowOf(event.target);
        var panel = panelOf(event.target);
        if ((!row && !panel) || row === drag.el) return;
        var list = null;
        var beforeRow = null;
        if (row) {
          list = row.parentNode;
          beforeRow = isBelowMiddle(event, row) ? row.nextElementSibling : row;
          if (beforeRow === drag.el) return;
        } else {
          list = panel.querySelector("[data-topic-list]");
        }
        if (!list || !list.getAttribute("data-topic-list")) return;
        event.preventDefault();
        if (!beforeRow && list === drag.el.parentNode && list.lastElementChild === drag.el) {
          return;
        }
        var sameTopic = drag.el.parentNode === list;
        list.insertBefore(drag.el, beforeRow);
        var params = new FormData();
        params.set("topic", list.getAttribute("data-topic-list"));
        params.set("before", beforeRow ? beforeRow.getAttribute("data-assignment-row") : "");
        postForm(drag.el.getAttribute("data-move-url"), params)
          .then(function () {
            if (!sameTopic) window.location.reload();
          })
          .catch(function (error) {
            window.alert(error.message || "Не удалось переместить задание");
            window.location.reload();
          });
        return;
      }

      if (drag.kind === "chapter") {
        // Порядок глав внутри класса.
        var targetChapter = chapterOf(event.target);
        if (!targetChapter || targetChapter === drag.el) return;
        if (targetChapter.getAttribute("data-block-id") !== drag.blockId) return;
        event.preventDefault();
        var beforeChapter = isBelowMiddle(event, targetChapter) ? targetChapter.nextElementSibling : targetChapter;
        if (beforeChapter === drag.el) return;
        var chapterContainer = targetChapter.parentNode;
        if (!beforeChapter && chapterContainer.lastElementChild === drag.el) return;
        var chapterParams = new FormData();
        chapterParams.set(
          "before",
          beforeChapter && beforeChapter.getAttribute("data-chapter-panel")
            ? beforeChapter.getAttribute("data-chapter-panel")
            : ""
        );
        chapterContainer.insertBefore(drag.el, beforeChapter);
        postForm(drag.el.getAttribute("data-move-url"), chapterParams).catch(function (error) {
          window.alert(error.message || "Не удалось переместить главу");
          window.location.reload();
        });
        return;
      }

      // TOPIC drop — порядок в главе, перенос в другую главу класса, копия в другой класс.
      var target = topicTarget(event);
      if (!target) return;
      event.preventDefault();
      var beforeId = target.before && target.before.getAttribute("data-topic-panel")
        ? target.before.getAttribute("data-topic-panel")
        : "";

      if (target.blockId && drag.blockId && target.blockId !== drag.blockId) {
        // COPY between classes
        if (!window.confirm("Скопировать тему «" + topicTitle(drag.el) + "» в другой класс? Оригинал сохранится, копия будет с черновиками заданий.")) {
          return;
        }
        var copyParams = new FormData();
        copyParams.set("target_block", target.blockId);
        if (target.chapterId) copyParams.set("target_chapter", target.chapterId);
        if (beforeId) copyParams.set("before", beforeId);
        drag.el.style.opacity = "0.5";
        postForm(drag.el.getAttribute("data-move-url"), copyParams)
          .then(function () {
            window.location.reload();
          })
          .catch(function (error) {
            window.alert(error.message || "Не удалось скопировать тему");
            window.location.reload();
          });
        return;
      }

      // MOVE within the same class (same or another chapter)
      if (!target.container) return;
      if (target.before === drag.el) return;
      var sameChapter = target.chapterId === drag.chapterId;
      if (sameChapter && !target.before && target.container.lastElementChild === drag.el) return;
      var topicParams = new FormData();
      topicParams.set("before", beforeId);
      if (target.chapterId) topicParams.set("target_chapter", target.chapterId);
      target.container.insertBefore(drag.el, target.before);
      postForm(drag.el.getAttribute("data-move-url"), topicParams)
        .then(function () {
          if (!sameChapter) window.location.reload();
        })
        .catch(function (error) {
          window.alert(error.message || "Не удалось переместить тему");
          window.location.reload();
        });
    });
  }

  // Форма темы: список глав фильтруется по выбранному классу.
  function chapterSelectFilter() {
    var blockSelect = document.querySelector("select[data-block-select]");
    var chapterSelect = document.querySelector("select[data-chapter-select]");
    if (!blockSelect || !chapterSelect) return;
    function apply() {
      var blockId = blockSelect.value;
      var current = chapterSelect.value;
      var keepCurrent = false;
      Array.prototype.forEach.call(chapterSelect.options, function (option) {
        var owner = option.getAttribute("data-block");
        if (!owner) {
          option.hidden = false;
          return;
        }
        var visible = !blockId || owner === blockId;
        option.hidden = !visible;
        option.disabled = !visible;
        if (visible && option.value === current) keepCurrent = true;
      });
      if (!keepCurrent) chapterSelect.value = "";
    }
    blockSelect.addEventListener("change", apply);
    apply();
  }

  // Скрытое значение с «глазом»: показать/скрыть и скопировать.
  function secretFields() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-secret]"), function (box) {
      var input = box.querySelector("[data-secret-input]");
      var toggle = box.querySelector("[data-secret-toggle]");
      var copy = box.querySelector("[data-secret-copy]");
      if (!input) return;
      if (toggle) {
        toggle.addEventListener("click", function () {
          var reveal = input.type === "password";
          input.type = reveal ? "text" : "password";
          toggle.setAttribute("aria-pressed", reveal ? "true" : "false");
          var label = reveal ? "Скрыть пароль" : "Показать пароль";
          toggle.setAttribute("title", label);
          toggle.setAttribute("aria-label", label);
          var use = toggle.querySelector("use");
          if (use) use.setAttribute("href", reveal ? "#i-eye-off" : "#i-eye");
        });
      }
      if (copy) {
        copy.addEventListener("click", function () {
          var value = input.value;
          var done = function () {
            var previous = copy.getAttribute("title");
            copy.setAttribute("title", "Скопировано");
            copy.classList.add("is-active");
            window.setTimeout(function () {
              copy.setAttribute("title", previous || "Скопировать");
              copy.classList.remove("is-active");
            }, 1500);
          };
          if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(value).then(done, function () {
              input.type = "text";
              input.select();
              document.execCommand && document.execCommand("copy");
              done();
            });
          } else {
            input.type = "text";
            input.select();
            document.execCommand && document.execCommand("copy");
            done();
          }
        });
      }
    });
  }

  function studentAssignmentPreview() {
    // Кнопка «Показать содержимое как видит ученик» в карточке ученика:
    // - первый клик грузит фрагмент через htmx (hx-get);
    // - повторные клики только скрывают/показывают уже загруженный контент.
    //
    // Подводный камень: htmx вешает свой обработчик click прямо на кнопку,
    // поэтому он срабатывает раньше нашего (на document) и шлёт GET при
    // каждом клике. Ответ приходил уже после того, как мы прятали блок, и
    // htmx:afterSwap снова его открывал — «Скрыть содержимое» будто не
    // работало. Теперь повторные запросы отменяются в htmx:configRequest,
    // а видимостью управляет состояние кнопки.
    var LABEL_SHOW = "Показать содержимое как видит ученик";
    var LABEL_HIDE = "Скрыть содержимое";

    function targetOf(button) {
      var id = button.getAttribute("aria-controls");
      return id ? document.getElementById(id) : null;
    }

    function isLoaded(target) {
      return !!target && target.innerHTML.trim().length > 0;
    }

    function setState(button, target, expanded) {
      var label = button.querySelector("[data-preview-label]");
      button.dataset.previewState = expanded ? "expanded" : "collapsed";
      button.setAttribute("aria-expanded", expanded ? "true" : "false");
      if (target) {
        if (expanded) target.removeAttribute("hidden");
        else target.setAttribute("hidden", "");
      }
      if (label) label.textContent = expanded ? LABEL_HIDE : LABEL_SHOW;
    }

    document.addEventListener("click", function (event) {
      var button = event.target.closest ? event.target.closest("[data-preview-toggle]") : null;
      if (!button) return;
      var target = targetOf(button);
      if (!target) return;
      event.preventDefault();
      if (button.dataset.previewState === "loading") return; // фрагмент уже в пути
      if (isLoaded(target)) {
        // Контент загружен — переключаем видимость сами: запрос отменён в
        // htmx:configRequest, поэтому ответ сервера не откроет блок обратно.
        setState(button, target, target.hasAttribute("hidden"));
        return;
      }
      // Первый клик: htmx выполнит hx-get и вставит фрагмент,
      // а htmx:afterSwap покажет блок и выставит подпись.
      button.dataset.previewState = "loading";
      button.setAttribute("aria-expanded", "true");
    });

    document.body.addEventListener("htmx:configRequest", function (event) {
      var button = event.detail && event.detail.elt;
      if (!button || !button.matches || !button.matches("[data-preview-toggle]")) return;
      // Повторный GET нужен только если фрагмент ещё не загружен и не в пути.
      if (button.dataset.previewState === "loading" || isLoaded(targetOf(button))) {
        event.preventDefault();
      }
    });

    document.body.addEventListener("htmx:afterSwap", function (event) {
      var target = event.detail && event.detail.target;
      if (!target || !target.classList || !target.classList.contains("assignment-preview-content")) {
        return;
      }
      var wrapper = target.closest ? target.closest("[data-assignment-preview]") : null;
      var button =
        (wrapper && wrapper.querySelector("[data-preview-toggle]")) ||
        (target.id ? document.querySelector('[aria-controls=\"' + target.id + '\"]') : null);
      if (!button) return;
      setState(button, target, true);
    });

    // Не удалось загрузить — возвращаем кнопку в исходное состояние,
    // чтобы преподаватель мог повторить запрос.
    Array.prototype.forEach.call(["htmx:responseError", "htmx:sendError", "htmx:swapError"], function (name) {
      document.body.addEventListener(name, function (event) {
        var button = event.detail && event.detail.elt;
        if (!button || !button.matches || !button.matches("[data-preview-toggle]")) return;
        setState(button, targetOf(button), false);
      });
    });
  }

  ready(function () {
    autohideAlerts();
    confirmForms();
    dangerZones();
    draftAutosave();
    flashcards();
    audioRecorder();
    quizItems();
    questionKindForms();
    document.body.addEventListener("htmx:afterSwap", function (event) {
      questionKindForms(event.detail && event.detail.target);
    });
    reviewPage();
    queueHelp();
    templatePresets();
    deadlineShortcuts();
    quizTiles();
    navToggle();
    cardPresetFill();
    presetLevelFilter();
    assignmentTypeForm();
    typeahead();
    uploadDropzones();
    chapterSelectFilter();
    secretFields();
    descriptionEditors();
    curriculumCollapse();
    curriculumBoard();
    studentAssignmentPreview();
  });
})();
