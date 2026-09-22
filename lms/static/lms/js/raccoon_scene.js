/* Живая сцена «Енот на опушке»: прогулка к книгам сквозь озеро, чтение и сон.
   Рисуется на canvas без emoji и сторонних библиотек; герой — мордочка енота. */
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

  /* ── Пейзаж ────────────────────────────────────────────── */

  function drawHills(ctx, w, h, drift) {
    ctx.fillStyle = "#cfe0d2";
    ctx.beginPath();
    ctx.moveTo(-20 + drift * 0.05, h * 0.62);
    ctx.quadraticCurveTo(w * 0.22, h * 0.16, w * 0.52, h * 0.58);
    ctx.quadraticCurveTo(w * 0.72, h * 0.34, w + 20, h * 0.6);
    ctx.lineTo(w + 20, h);
    ctx.lineTo(-20, h);
    ctx.closePath();
    ctx.fill();

    ctx.fillStyle = "#bcd6c1";
    ctx.beginPath();
    ctx.moveTo(-20, h * 0.72);
    ctx.quadraticCurveTo(w * 0.3, h * 0.44, w * 0.66, h * 0.7);
    ctx.quadraticCurveTo(w * 0.86, h * 0.54, w + 20, h * 0.68);
    ctx.lineTo(w + 20, h);
    ctx.lineTo(-20, h);
    ctx.closePath();
    ctx.fill();
  }

  function drawTree(ctx, x, baseY, height, sway) {
    ctx.save();
    ctx.translate(x + sway, baseY);
    ctx.fillStyle = "#6b4a2b";
    ctx.fillRect(-1.1, -height * 0.26, 2.2, height * 0.26);
    ctx.fillStyle = "#3f6b4a";
    ctx.beginPath();
    ctx.moveTo(0, -height);
    ctx.lineTo(height * 0.3, -height * 0.24);
    ctx.lineTo(-height * 0.3, -height * 0.24);
    ctx.closePath();
    ctx.fill();
    ctx.fillStyle = "#4f855c";
    ctx.beginPath();
    ctx.moveTo(0, -height * 0.78);
    ctx.lineTo(height * 0.22, -height * 0.34);
    ctx.lineTo(-height * 0.22, -height * 0.34);
    ctx.closePath();
    ctx.fill();
    ctx.restore();
  }

  function drawHouse(ctx, x, baseY, w, h) {
    var wallH = h * 0.6;
    var roofH = h - wallH;
    ctx.save();
    ctx.translate(x, baseY - h);
    ctx.fillStyle = "#d3bba0";
    ctx.fillRect(1, roofH, w - 2, wallH);
    ctx.fillStyle = "#7c5a45";
    ctx.beginPath();
    ctx.moveTo(-1, roofH + 1.5);
    ctx.lineTo(w / 2, 0);
    ctx.lineTo(w + 1, roofH + 1.5);
    ctx.closePath();
    ctx.fill();
    ctx.fillStyle = "#5c3a24";
    ctx.fillRect(w * 0.4, roofH + wallH * 0.3, w * 0.2, wallH * 0.7);
    ctx.fillStyle = "#dfeaf0";
    ctx.fillRect(w * 0.14, roofH + wallH * 0.26, w * 0.17, wallH * 0.34);
    ctx.fillStyle = "#8a5f3f";
    ctx.fillRect(w * 0.52, -roofH * 0.1, w * 0.06, roofH * 0.9);
    ctx.restore();
  }

  function drawReeds(ctx, x, baseY, height, sway) {
    for (var i = 0; i < 3; i += 1) {
      var offset = i * 2.6 - 2.6;
      ctx.strokeStyle = i === 1 ? "#5c8a66" : "#7ba379";
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      ctx.moveTo(x + offset, baseY);
      ctx.quadraticCurveTo(x + offset + sway, baseY - height * 0.6, x + offset + sway * 1.6, baseY - height);
      ctx.stroke();
      ctx.fillStyle = "#8a5f3f";
      ctx.beginPath();
      ctx.ellipse(x + offset + sway * 1.6, baseY - height - 1, 1.3, 3, 0, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  function drawFrog(ctx, x, y) {
    ctx.fillStyle = "#5f9a63";
    ctx.beginPath();
    ctx.ellipse(x, y, 4, 3.1, 0, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "#f4f2ea";
    ctx.beginPath();
    ctx.arc(x - 1.6, y - 2.4, 1.35, 0, Math.PI * 2);
    ctx.arc(x + 1.6, y - 2.4, 1.35, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "#26302a";
    ctx.beginPath();
    ctx.arc(x - 1.6, y - 2.4, 0.6, 0, Math.PI * 2);
    ctx.arc(x + 1.6, y - 2.4, 0.6, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = "#3f6b4a";
    ctx.lineWidth = 0.9;
    ctx.beginPath();
    ctx.arc(x, y + 0.4, 1.7, 0.2 * Math.PI, 0.8 * Math.PI);
    ctx.stroke();
  }

  function drawDuck(ctx, x, y, bob) {
    ctx.save();
    ctx.translate(x, y + bob);
    ctx.fillStyle = "#efe7d5";
    ctx.beginPath();
    ctx.ellipse(0, 0, 6, 4.2, 0, 0, Math.PI * 2);
    ctx.fill();
    ctx.beginPath();
    ctx.arc(4.4, -3.6, 3, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "#7c5a45";
    ctx.beginPath();
    ctx.moveTo(6.4, -4.6);
    ctx.lineTo(10, -3.9);
    ctx.lineTo(6.4, -3);
    ctx.closePath();
    ctx.fill();
    ctx.fillStyle = "#3f454d";
    ctx.beginPath();
    ctx.arc(5.2, -4.4, 0.7, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "#d8c7a6";
    ctx.beginPath();
    ctx.ellipse(-2.6, 0.6, 3, 2.4, -0.3, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
  }

  function drawBench(ctx, x, baseY, w) {
    var seatY = baseY - 9;
    ctx.fillStyle = "#8a5f3f";
    ctx.fillRect(x, seatY, w, 2.6);
    ctx.fillStyle = "#a9793f";
    ctx.fillRect(x, seatY + 3.4, w, 1.8);
    ctx.fillStyle = "#7c5a45";
    ctx.fillRect(x + 1.5, seatY + 5, 2.2, 5.6);
    ctx.fillRect(x + w - 3.7, seatY + 5, 2.2, 5.6);
  }

  function drawBushes(ctx, w, groundY, h, drift) {
    var spots = [0.17, 0.26, 0.35, 0.63, 0.78, 0.86];
    var flowers = [0.21, 0.31, 0.66, 0.83, 0.9];
    ctx.save();
    spots.forEach(function (spot, index) {
      var x = w * spot + drift * (index % 2 ? 0.3 : 0.2);
      var radius = h * (index % 3 === 0 ? 0.13 : 0.1);
      ctx.fillStyle = index % 2 ? "#6f9c74" : "#5f8f68";
      ctx.beginPath();
      ctx.arc(x, groundY + 1, radius, Math.PI, 0);
      ctx.arc(x + radius * 0.9, groundY + 1, radius * 0.72, Math.PI, 0);
      ctx.fill();
    });
    flowers.forEach(function (spot, index) {
      var x = w * spot + drift * 0.25;
      var y = groundY + h * 0.16 + (index % 2) * h * 0.12;
      ctx.strokeStyle = "#5f8f68";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, y + 3.4);
      ctx.lineTo(x, y);
      ctx.stroke();
      ctx.fillStyle = index % 2 ? "#f0c987" : "#f4f0e6";
      ctx.beginPath();
      ctx.arc(x, y - 0.6, 1.5, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "#e0a24a";
      ctx.beginPath();
      ctx.arc(x, y - 0.6, 0.6, 0, Math.PI * 2);
      ctx.fill();
    });
    ctx.restore();
  }

  function drawPage(ctx, x, y, angle, alpha) {
    ctx.save();
    ctx.translate(x, y);
    ctx.rotate(angle);
    ctx.globalAlpha = alpha;
    ctx.fillStyle = "#fdfaf3";
    ctx.strokeStyle = "#d8cbb4";
    ctx.lineWidth = 0.7;
    ctx.beginPath();
    ctx.rect(-4.6, -3.2, 9.2, 6.4);
    ctx.fill();
    ctx.stroke();
    ctx.strokeStyle = "#c3b7a3";
    ctx.beginPath();
    ctx.moveTo(-3, -1.2);
    ctx.lineTo(3, -1.2);
    ctx.moveTo(-3, 0.9);
    ctx.lineTo(1.6, 0.9);
    ctx.stroke();
    ctx.restore();
  }

  /* ── Сюжет ─────────────────────────────────────────────── */

  var CYCLE = 56000;
  var HOME = 0.095;
  var SHORE_LEFT = 0.5;
  var SHORE_RIGHT = 0.7;
  var BENCH = 0.88;

  function sceneState(t) {
    var p = (t % CYCLE) / CYCLE;
    var state = { x: HOME, facing: 1, pose: "sleep", lift: 0 };
    var leg = function (from, to, a, b) {
      var k = (p - a) / (b - a);
      return from + (to - from) * Math.min(1, Math.max(0, k));
    };
    if (p < 0.06) {
      state.pose = "sleep";
    } else if (p < 0.28) {
      state.x = leg(HOME, SHORE_LEFT, 0.06, 0.28);
      state.pose = "walk";
    } else if (p < 0.4) {
      state.x = leg(SHORE_LEFT, SHORE_RIGHT, 0.28, 0.4);
      state.pose = "swim";
    } else if (p < 0.46) {
      state.x = leg(SHORE_RIGHT, BENCH, 0.4, 0.46);
      state.pose = "walk";
    } else if (p < 0.62) {
      state.x = BENCH;
      state.pose = "read";
    } else if (p < 0.68) {
      state.x = leg(BENCH, SHORE_RIGHT, 0.62, 0.68);
      state.facing = -1;
      state.pose = "walk";
    } else if (p < 0.8) {
      state.x = leg(SHORE_RIGHT, SHORE_LEFT, 0.68, 0.8);
      state.facing = -1;
      state.pose = "swim";
    } else if (p < 0.94) {
      state.x = leg(SHORE_LEFT, HOME, 0.8, 0.94);
      state.facing = -1;
      state.pose = "walk";
    } else {
      state.facing = -1;
      state.pose = "sleep";
    }
    state.p = p;
    return state;
  }

  function start(root) {
    var canvas = root.querySelector("canvas");
    if (!canvas || !canvas.getContext) return;
    var reduced =
      window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var faceImg = null;
    var sleepImg = null;
    var booksImg = null;
    var startAt = performance.now();
    var trees = [
      { x: 0.03, h: 0.3, layer: 0.22 },
      { x: 0.11, h: 0.22, layer: 0.34 },
      { x: 0.2, h: 0.34, layer: 0.18 },
      { x: 0.29, h: 0.24, layer: 0.4 },
      { x: 0.38, h: 0.31, layer: 0.26 },
      { x: 0.62, h: 0.27, layer: 0.3 },
      { x: 0.72, h: 0.2, layer: 0.42 },
      { x: 0.81, h: 0.32, layer: 0.24 },
      { x: 0.92, h: 0.23, layer: 0.36 }
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
      var drift = reduced ? 0 : Math.sin(t / 4000) * 5;
      var cloud = reduced ? w * 0.4 : (t / 90) % (w + 60);
      var groundY = h - Math.max(7, h * 0.14);
      var lakeLeft = w * 0.45;
      var lakeRight = w * 0.75;
      var lakeTop = groundY - Math.max(4, h * 0.1);

      drawHills(ctx, w, h, drift);

      ctx.fillStyle = "rgba(255,251,235,0.95)";
      ctx.beginPath();
      ctx.arc(w * 0.9, h * 0.24, h * 0.11 + 2, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "rgba(255,255,255,0.7)";
      ctx.beginPath();
      ctx.arc(cloud - 22, h * 0.3, h * 0.07, 0, Math.PI * 2);
      ctx.arc(cloud - 12, h * 0.26, h * 0.055, 0, Math.PI * 2);
      ctx.fill();

      trees.forEach(function (tree) {
        var sway = reduced ? 0 : Math.sin(t / 900 + tree.x * 8) * 1.3;
        drawTree(ctx, w * tree.x + drift * tree.layer, groundY, h * tree.h, sway);
      });

      /* Озеро: вода и камыши по берегам */
      var water = ctx.createLinearGradient(0, lakeTop, 0, groundY + 2);
      water.addColorStop(0, "#9ed3dd");
      water.addColorStop(1, "#6fb4c7");
      ctx.fillStyle = water;
      ctx.beginPath();
      ctx.moveTo(lakeLeft - w * 0.02, lakeTop);
      ctx.quadraticCurveTo((lakeLeft + lakeRight) / 2, lakeTop - h * 0.05, lakeRight + w * 0.02, lakeTop);
      ctx.lineTo(lakeRight + w * 0.02, groundY + 3);
      ctx.lineTo(lakeLeft - w * 0.02, groundY + 3);
      ctx.closePath();
      ctx.fill();
      var ripple = reduced ? 0 : Math.sin(t / 700) * 1.4;
      ctx.strokeStyle = "rgba(255,255,255,0.7)";
      ctx.lineWidth = 1;
      for (var r = 0; r < 3; r += 1) {
        var ry = lakeTop + h * 0.03 + r * Math.max(2.4, h * 0.05);
        ctx.beginPath();
        ctx.moveTo(lakeLeft + w * 0.02 + ripple + r * 3, ry);
        ctx.lineTo(lakeLeft + w * 0.09 + ripple + r * 3, ry);
        ctx.stroke();
      }
      drawReeds(ctx, lakeLeft - w * 0.015, groundY + 4, h * 0.34, reduced ? 0 : Math.sin(t / 800) * 1.6);
      drawReeds(ctx, lakeRight + w * 0.012, groundY + 4, h * 0.3, reduced ? 0 : Math.sin(t / 760 + 1) * 1.6);
      drawDuck(ctx, (lakeLeft + lakeRight) / 2 + w * 0.04, lakeTop + h * 0.06, reduced ? 0 : Math.sin(t / 620) * 1.6);
      drawFrog(ctx, lakeRight + w * 0.035, groundY + 2);
      drawFrog(ctx, lakeLeft - w * 0.04, groundY + 2.4);

      /* Земля: трава и тропинка */
      ctx.fillStyle = "#7fa786";
      ctx.fillRect(0, groundY, w, h - groundY);
      ctx.fillStyle = "#ead9c0";
      ctx.fillRect(w * 0.03, groundY + Math.max(2, (h - groundY) * 0.3), w * 0.94, Math.max(2, (h - groundY) * 0.28));

      drawBushes(ctx, w, groundY, h, drift);
      drawHouse(ctx, 5, groundY + 2, h * 0.52, h * 0.5);
      var benchW = h * 0.6;
      drawBench(ctx, w - benchW - 6, groundY + 2, benchW);
      if (booksImg) {
        var bookW = h * 0.42;
        var bookH = bookW * (34 / 44);
        ctx.drawImage(booksImg, w - benchW * 0.52 - bookW / 2, groundY - 9 - bookH * 0.86, bookW, bookH);
      }

      var state = reduced ? { x: HOME, facing: 1, pose: "sleep", p: 1 } : sceneState(t);
      var size = Math.max(28, Math.min(h * 0.84, 46));
      var noiseX = reduced ? 0 : Math.sin(t / 180) * 1.1;
      var bounce =
        state.pose === "walk" && !reduced ? Math.abs(Math.sin(t / 95)) * 1.8 : 0;
      var petX = state.x * (w - size) + noiseX;
      var petY = h - size - (h - groundY) * 0.42 - bounce;
      if (state.pose === "swim") petY += h * 0.12;
      if (state.pose === "sleep") petY += h * 0.05;
      petY = Math.max(0, Math.min(petY, h - size));

      ctx.fillStyle = "rgba(63,90,71,0.16)";
      ctx.beginPath();
      ctx.ellipse(petX + size / 2, petY + size * 0.94, size * 0.34, size * 0.08, 0, 0, Math.PI * 2);
      ctx.fill();

      var image = state.pose === "sleep" && sleepImg ? sleepImg : faceImg;
      ctx.save();
      ctx.translate(petX + size / 2, petY + size / 2);
      ctx.scale(state.facing, 1);
      if (state.pose === "sleep") ctx.rotate(0.12);
      if (state.pose === "read") ctx.rotate(-0.06);
      if (image) {
        ctx.drawImage(image, -size / 2, -size / 2, size, size);
      } else {
        ctx.fillStyle = "#98a4ae";
        ctx.beginPath();
        ctx.arc(0, 0, size / 2, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.restore();

      if (state.pose === "read" && !reduced) {
        ctx.fillStyle = "#c45c3a";
        ctx.fillRect(petX + size * 0.12, petY + size * 0.74, size * 0.62, size * 0.18);
        ctx.fillStyle = "#f2e6cf";
        ctx.fillRect(petX + size * 0.16, petY + size * 0.77, size * 0.54, size * 0.11);
        for (var page = 0; page < 3; page += 1) {
          var pageT = (t / 2600 + page * 0.34) % 1;
          drawPage(
            ctx,
            petX + size * 0.5 + pageT * w * 0.09,
            petY + size * 0.4 - pageT * h * 0.34,
            pageT * 2.4,
            Math.max(0, 1 - pageT * 1.15)
          );
        }
      }

      if (state.pose === "swim") {
        /* Вода поверх мордочки: видно, что енот плывёт */
        ctx.fillStyle = "rgba(122,187,203,0.72)";
        ctx.beginPath();
        ctx.moveTo(petX - w * 0.01, petY + size * 0.72);
        ctx.quadraticCurveTo(petX + size * 0.5, petY + size * 0.62, petX + size + w * 0.01, petY + size * 0.72);
        ctx.lineTo(petX + size + w * 0.01, petY + size + 4);
        ctx.lineTo(petX - w * 0.01, petY + size + 4);
        ctx.closePath();
        ctx.fill();
        ctx.strokeStyle = "rgba(255,255,255,0.85)";
        ctx.lineWidth = 1.1;
        ctx.beginPath();
        ctx.moveTo(petX + size * 0.62, petY + size * 0.74);
        ctx.quadraticCurveTo(petX + size * 0.9, petY + size * 0.68, petX + size * 1.2, petY + size * 0.76);
        ctx.stroke();
      }

      if (state.pose === "sleep" && !reduced) {
        var z = 0.5 + 0.5 * Math.sin(t / 600);
        ctx.fillStyle = "rgba(63,90,71," + (0.35 + 0.5 * z) + ")";
        ctx.font = "700 " + Math.round(h * 0.21) + "px sans-serif";
        ctx.fillText("z", petX + size * 0.92, petY + size * 0.34);
        ctx.font = "700 " + Math.round(h * 0.26) + "px sans-serif";
        ctx.fillText("Z", petX + size * 1.06, petY + size * 0.1);
      }

      if (!reduced) requestAnimationFrame(draw);
    }

    resize();
    window.addEventListener("resize", resize);
    Promise.all([
      loadImage(root.getAttribute("data-pet-src")),
      loadImage(root.getAttribute("data-sleep-src")),
      loadImage(root.getAttribute("data-books-src"))
    ]).then(function (images) {
      faceImg = images[0];
      sleepImg = images[1];
      booksImg = images[2];
      requestAnimationFrame(draw);
    });
  }

  ready(function () {
    var root = document.querySelector("[data-raccoon-scene]");
    if (root) start(root);
  });
})();
