/* Праздничная открытка ко Дню учителя: фокус, закрытие и лёгкое конфетти. */
(function () {
  "use strict";

  function ready(callback) {
    if (document.readyState !== "loading") callback();
    else document.addEventListener("DOMContentLoaded", callback);
  }

  ready(function () {
    var celebration = document.querySelector("[data-teacher-day-card]");
    if (!celebration) return;

    var card = celebration.querySelector("[role=dialog]");
    var closeButtons = celebration.querySelectorAll("[data-teacher-day-close]");
    var previousFocus = document.activeElement;
    var isClosing = false;
    var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    document.body.classList.add("teacher-day-is-open");
    document.body.style.overflow = "hidden";

    function addConfetti() {
      if (reduceMotion) return;
      var container = celebration.querySelector("[data-teacher-day-confetti]");
      if (!container) return;
      var colors = ["#d86e3e", "#2a9d94", "#e7b94f", "#5c8a66", "#8b79b9", "#e0897c"];
      var fragment = document.createDocumentFragment();
      for (var i = 0; i < 82; i += 1) {
        var piece = document.createElement("i");
        piece.className = "teacher-day-confetti__piece";
        piece.style.setProperty("--confetti-left", Math.round(Math.random() * 100) + "%");
        piece.style.setProperty("--confetti-size", 7 + Math.round(Math.random() * 8) + "px");
        piece.style.setProperty("--confetti-color", colors[i % colors.length]);
        piece.style.setProperty("--confetti-rotation", Math.round(Math.random() * 180) + "deg");
        piece.style.setProperty("--confetti-drift", -80 + Math.round(Math.random() * 160) + "px");
        piece.style.setProperty("--confetti-duration", 2600 + Math.round(Math.random() * 1900) + "ms");
        piece.style.setProperty("--confetti-delay", Math.round(Math.random() * 850) + "ms");
        fragment.appendChild(piece);
      }
      container.appendChild(fragment);
    }

    function closeCard() {
      if (isClosing) return;
      isClosing = true;
      document.removeEventListener("keydown", onKeydown);
      celebration.classList.add("is-closing");
      document.body.classList.remove("teacher-day-is-open");
      document.body.style.overflow = "";
      window.setTimeout(function () {
        if (celebration.parentNode) celebration.parentNode.removeChild(celebration);
        if (previousFocus && previousFocus.focus) previousFocus.focus();
      }, reduceMotion ? 0 : 270);
    }

    function focusableItems() {
      return celebration.querySelectorAll('button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])');
    }

    function onKeydown(event) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeCard();
        return;
      }
      if (event.key !== "Tab") return;
      var items = focusableItems();
      if (!items.length) {
        event.preventDefault();
        card.focus();
        return;
      }
      var first = items[0];
      var last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    Array.prototype.forEach.call(closeButtons, function (button) {
      button.addEventListener("click", closeCard);
    });
    document.addEventListener("keydown", onKeydown);
    addConfetti();
    window.setTimeout(function () {
      var primary = celebration.querySelector(".teacher-day-card__button");
      (primary || card).focus();
    }, reduceMotion ? 0 : 420);
  });
})();
