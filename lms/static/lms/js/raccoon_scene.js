/* Живая сцена енота: прогулка к книгам, чтение, сон. Без emoji и сторонних библиотек. */
(function () {
  "use strict";

  function ready(fn) {
    if (document.readyState !== "loading") fn();
    else document.addEventListener("DOMContentLoaded", fn);
  }

  function loadImage(src) {
    return new Promise(function (resolve) {
      if (!src) {
        resolve(null);
        return;
      }
      var img = new Image();
      img.onload = function () {
        resolve(img);
      };
      img.onerror = function () {
        resolve(null);
      };
      img.src = src;
    });
  }

  function drawHouse(ctx, x, y, w, h) {
    /* Домик без мордочки, размер как стопка книг (~44×34). */
    var wallH = h * 0.58;
    var roofH = h - wallH;
    ctx.save();
    ctx.translate(x, y);
    ctx.fillStyle = "#d3bba0";
    ctx.fillRect(2, roofH, w - 4, wallH);
    ctx.fillStyle = "#7c5a45";
    ctx.beginPath();
    ctx.moveTo(0, roofH + 2);
    ctx.lineTo(w / 2, 0);
    ctx.lineTo(w, roofH + 2);
    ctx.closePath();
    ctx.fill();
    ctx.fillStyle = "#5c3a24";
    ctx.fillRect(w * 0.4, roofH + wallH * 0.32, w * 0.2, wallH * 0.68);
    ctx.fillStyle = "#dfeaf0";
    ctx.fillRect(w * 0.14, roofH + wallH * 0.28, w * 0.18, wallH * 0.32);
    ctx.restore();
  }

  function drawTree(ctx, x, baseY, height, sway) {
    ctx.save();
    ctx.translate(x + sway, baseY);
    ctx.fillStyle = "#6b4a2b";
    ctx.fillRect(-1.5, -height * 0.22, 3, height * 0.22);
    ctx.fillStyle = "#3f6b4a";
    ctx.beginPath();
    ctx.moveTo(0, -height);
    ctx.lineTo(height * 0.28, -height * 0.22);
    ctx.lineTo(-height * 0.28, -height * 0.22);
    ctx.closePath();
    ctx.fill();
    ctx.fillStyle = "#4f855c";
    ctx.beginPath();
    ctx.moveTo(0, -height * 0.82);
    ctx.lineTo(height * 0.2, -height * 0.32);
    ctx.lineTo(-height * 0.2, -height * 0.32);
    ctx.closePath();
    ctx.fill();
    ctx.restore();
  }

  function sceneState(t) {
    var cycle = 52000;
    var p = (t % cycle) / cycle;
    var home = 0.04;
    var books = 0.76;
    var x = home;
    var facing = 1;
    var pose = "walk";
    if (p < 0.154) {
      var k = p / 0.154;
      x = home + (books - home) * k;
      facing = 1;
      pose = "walk";
    } else if (p < 0.25) {
      x = books;
      facing = 1;
      pose = "read";
    } else if (p < 0.404) {
      var k2 = (p - 0.25) / 0.154;
      x = books + (home - books) * k2;
      facing = -1;
      pose = "walk";
    } else {
      x = home;
      facing = -1;
      pose = "sleep";
    }
    return { x: x, facing: facing, pose: pose, p: p };
  }

  function start(root) {
    var canvas = root.querySelector("canvas");
    if (!canvas || !canvas.getContext) return;
    var reduced =
      window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var petImg = null;
    var booksImg = null;
    var startAt = performance.now();
    var trees = [
      { x: 0.08, h: 22, layer: 0.25 },
      { x: 0.18, h: 16, layer: 0.4 },
      { x: 0.32, h: 26, layer: 0.2 },
      { x: 0.48, h: 15, layer: 0.45 },
      { x: 0.62, h: 24, layer: 0.3 },
      { x: 0.78, h: 17, layer: 0.5 }
    ];

    function resize() {
      var ratio = window.devicePixelRatio || 1;
      var w = Math.max(1, root.clientWidth);
      var h = Math.max(1, root.clientHeight);
      canvas.width = Math.round(w * ratio);
      canvas.height = Math.round(h * ratio);
    }

    function draw(now) {
      var ctx = canvas.getContext("2d");
      var ratio = window.devicePixelRatio || 1;
      var w = canvas.width / ratio;
      var h = canvas.height / ratio;
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      ctx.clearRect(0, 0, w, h);

      var t = reduced ? 0 : now - startAt;
      var drift = reduced ? 0 : Math.sin(t / 4000) * 6;
      var cloud = reduced ? 0 : (t / 80) % (w + 40);

      ctx.fillStyle = "rgba(255,251,235,0.95)";
      ctx.beginPath();
      ctx.arc(w * 0.88, h * 0.22, 7, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "rgba(255,255,255,0.75)";
      ctx.beginPath();
      ctx.arc(cloud - 20, h * 0.28, 5, 0, Math.PI * 2);
      ctx.arc(cloud - 12, h * 0.24, 4, 0, Math.PI * 2);
      ctx.fill();

      var groundY = h - 14;
      trees.forEach(function (tree) {
        var sway = reduced ? 0 : Math.sin(t / 900 + tree.x * 8) * 1.4;
        drawTree(ctx, w * tree.x + drift * tree.layer, groundY, tree.h, sway);
      });

      ctx.fillStyle = "#7fa786";
      ctx.fillRect(0, h - 14, w, 14);
      ctx.fillStyle = "#ead9c0";
      ctx.fillRect(w * 0.04, h - 7, w * 0.9, 4);

      drawHouse(ctx, 6, h - 14 - 34, 44, 34);
      if (booksImg) {
        ctx.drawImage(booksImg, w - 50, h - 14 - 34, 44, 34);
      } else {
        ctx.fillStyle = "#c45c3a";
        ctx.fillRect(w - 48, h - 28, 14, 16);
        ctx.fillStyle = "#3f5a47";
        ctx.fillRect(w - 36, h - 32, 14, 20);
        ctx.fillStyle = "#d9b95f";
        ctx.fillRect(w - 24, h - 26, 14, 14);
      }

      var state = reduced ? { x: 0.04, facing: 1, pose: "sleep", p: 1 } : sceneState(t);
      var petW = 52;
      var petH = 36;
      var noiseX = reduced ? 0 : Math.sin(t / 180) * 1.2;
      var bounce =
        state.pose === "walk" && !reduced ? Math.abs(Math.sin(t / 90)) * 2.4 : 0;
      var petX = state.x * (w - petW) + noiseX;
      var petY = h - 8 - petH - bounce;
      if (state.pose === "sleep") petY += 3;

      ctx.save();
      ctx.translate(petX + petW / 2, petY + petH / 2);
      ctx.scale(state.facing, 1);
      if (state.pose === "sleep") ctx.rotate(0.16);
      if (petImg) {
        ctx.drawImage(petImg, -petW / 2, -petH / 2, petW, petH);
      } else {
        ctx.fillStyle = "#d0783a";
        ctx.fillRect(-petW / 2, -petH / 4, petW * 0.7, petH * 0.45);
      }
      ctx.restore();

      if (state.pose === "read" && !reduced) {
        ctx.fillStyle = "#3f5a47";
        ctx.fillRect(petX + 18, petY - 8, 12, 9);
      }
      if (state.pose === "sleep" && !reduced) {
        var z = 0.5 + 0.5 * Math.sin(t / 600);
        ctx.fillStyle = "rgba(63,90,71," + (0.35 + 0.5 * z) + ")";
        ctx.font = "700 11px sans-serif";
        ctx.fillText("z", petX + 40, petY + 4);
        ctx.font = "700 13px sans-serif";
        ctx.fillText("Z", petX + 48, petY - 4);
      }

      if (!reduced) requestAnimationFrame(draw);
    }

    resize();
    window.addEventListener("resize", resize);
    Promise.all([loadImage(root.getAttribute("data-pet-src")), loadImage(root.getAttribute("data-books-src"))]).then(
      function (images) {
        petImg = images[0];
        booksImg = images[1];
        requestAnimationFrame(draw);
      }
    );
  }

  ready(function () {
    var root = document.querySelector("[data-raccoon-scene]");
    if (root) start(root);
  });
})();
