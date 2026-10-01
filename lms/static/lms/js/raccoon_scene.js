/* Живая сцена «Енот на опушке»: прогулка к книгам сквозь озеро, чтение и сон.
   Сцена меняется по времени года: весна — тюльпаны, цветущие кусты, бабочки
   и лепестки; лето — зелень, ромашки, утка и лягушки; осень — янтарные кроны,
   листопад, тыква и журавлиный клин; зима — снегопад, снеговик, дымок из трубы
   и замёрзшее озеро, по которому енот скользит на коньках. Сезон определяется
   по текущему месяцу в браузере; для просмотра можно задать принудительно
   ?season=spring|summer|autumn|winter или атрибут data-season на корневом
   элементе. Рисуется на canvas без emoji и сторонних библиотек. */
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

  /* ── Времена года ──────────────────────────────────────
     Палитры и реквизит каждого сезона; лето повторяет
     исходный облик сцены, остальные сезоны — его вариации. */

  var SEASONS = {
    spring: {
      hillsFar: "#d7e9cd",
      hillsNear: "#c5dfba",
      grass: "#89b088",
      path: "#ecdfc4",
      water: ["#a8d9e3", "#7abecb"],
      trees: [
        ["#3f6b4a", "#4f855c"],
        ["#558d5c", "#68a06a"],
        ["#467750", "#599461"]
      ],
      bushA: "#7bab7c",
      bushB: "#68996a",
      reeds: ["#5c8a66", "#7ba379", "#8a5f3f"],
      sun: "rgba(255,251,235,0.95)",
      flowers: "tulip",
      blossoms: true,
      petals: true,
      butterflies: true,
      duck: true,
      frogs: true,
      aria:
        "Мордочка енота в шарфе бежит по весенней опушке мимо тюльпанов и бабочек к книгам, переплывает озеро, читает их и засыпает"
    },
    summer: {
      hillsFar: "#cfe0d2",
      hillsNear: "#bcd6c1",
      grass: "#7fa786",
      path: "#ead9c0",
      water: ["#9ed3dd", "#6fb4c7"],
      trees: [["#3f6b4a", "#4f855c"]],
      bushA: "#6f9c74",
      bushB: "#5f8f68",
      reeds: ["#5c8a66", "#7ba379", "#8a5f3f"],
      sun: "rgba(255,251,235,0.95)",
      flowers: "daisy",
      duck: true,
      frogs: true,
      aria:
        "Мордочка енота в шарфе бежит по опушке к книгам, переплывает озеро, читает их и засыпает"
    },
    autumn: {
      hillsFar: "#e2d9bd",
      hillsNear: "#d8cba6",
      grass: "#9aa572",
      path: "#e9d7b2",
      water: ["#a3c0c5", "#7c9ea6"],
      trees: [
        ["#4a734a", "#5d8b58"],
        ["#b06a33", "#ca8b45"],
        ["#c19137", "#d3a94e"],
        ["#8e5a2e", "#a9713f"],
        ["#4a734a", "#5d8b58"],
        ["#a4552f", "#bd6f3c"]
      ],
      bushA: "#8f9a5c",
      bushB: "#7b8a50",
      reeds: ["#8a7a4e", "#a2915c", "#7c5a3d"],
      sun: "rgba(255,240,205,0.92)",
      leavesFall: true,
      groundLeaves: true,
      floatingLeaves: true,
      geese: true,
      pumpkin: true,
      mushrooms: true,
      duck: true,
      frogs: false,
      aria:
        "Мордочка енота в шарфе бежит по осенней опушке среди падающих листьев к книгам, переплывает озеро, читает их и засыпает"
    },
    winter: {
      hillsFar: "#edf3f7",
      hillsNear: "#e1ebf2",
      grass: "#eef4f7",
      path: "#dde8f0",
      ice: ["#e2eff6", "#c3dcea"],
      trees: [
        ["#3c644c", "#4e7c5e"],
        ["#345a44", "#477156"],
        ["#416d52", "#548565"]
      ],
      bushA: "#dbe7ec",
      bushB: "#ccdbe3",
      reeds: ["#9db2b6", "#b4c6c8", "#7c5f47"],
      sun: "rgba(252,248,238,0.72)",
      frozen: true,
      snowfall: true,
      snowTrees: true,
      snowRoof: true,
      snowBench: true,
      snowman: true,
      smoke: true,
      duck: false,
      frogs: false,
      aria:
        "Мордочка енота в шарфе бежит по снежной опушке к книгам, скользит на коньках через замёрзшее озеро, читает их и засыпает"
    }
  };

  function detectSeason(root) {
    var forced = root.getAttribute("data-season");
    if (forced && SEASONS[forced]) return forced;
    var match = /[?&]season=(spring|summer|autumn|winter)/.exec(window.location.search);
    if (match) return match[1];
    var month = new Date().getMonth();
    if (month === 11 || month < 2) return "winter";
    if (month < 5) return "spring";
    if (month < 8) return "summer";
    return "autumn";
  }

  /* ── Пейзаж ────────────────────────────────────────────── */

  function drawHills(ctx, w, h, drift, cfg) {
    ctx.fillStyle = cfg.hillsFar;
    ctx.beginPath();
    ctx.moveTo(-20 + drift * 0.05, h * 0.62);
    ctx.quadraticCurveTo(w * 0.22, h * 0.16, w * 0.52, h * 0.58);
    ctx.quadraticCurveTo(w * 0.72, h * 0.34, w + 20, h * 0.6);
    ctx.lineTo(w + 20, h);
    ctx.lineTo(-20, h);
    ctx.closePath();
    ctx.fill();

    ctx.fillStyle = cfg.hillsNear;
    ctx.beginPath();
    ctx.moveTo(-20, h * 0.72);
    ctx.quadraticCurveTo(w * 0.3, h * 0.44, w * 0.66, h * 0.7);
    ctx.quadraticCurveTo(w * 0.86, h * 0.54, w + 20, h * 0.68);
    ctx.lineTo(w + 20, h);
    ctx.lineTo(-20, h);
    ctx.closePath();
    ctx.fill();
  }

  function drawTree(ctx, x, baseY, height, sway, colors, snow) {
    ctx.save();
    ctx.translate(x + sway, baseY);
    ctx.fillStyle = "#6b4a2b";
    ctx.fillRect(-1.1, -height * 0.26, 2.2, height * 0.26);
    ctx.fillStyle = colors[0];
    ctx.beginPath();
    ctx.moveTo(0, -height);
    ctx.lineTo(height * 0.3, -height * 0.24);
    ctx.lineTo(-height * 0.3, -height * 0.24);
    ctx.closePath();
    ctx.fill();
    ctx.fillStyle = colors[1];
    ctx.beginPath();
    ctx.moveTo(0, -height * 0.78);
    ctx.lineTo(height * 0.22, -height * 0.34);
    ctx.lineTo(-height * 0.22, -height * 0.34);
    ctx.closePath();
    ctx.fill();
    if (snow) {
      ctx.fillStyle = "rgba(248,251,253,0.95)";
      ctx.beginPath();
      ctx.moveTo(0, -height);
      ctx.lineTo(height * 0.09, -height * 0.87);
      ctx.lineTo(-height * 0.09, -height * 0.87);
      ctx.closePath();
      ctx.fill();
      ctx.beginPath();
      ctx.ellipse(0, -height * 0.235, height * 0.22, height * 0.05, 0, Math.PI, Math.PI * 2);
      ctx.fill();
      ctx.beginPath();
      ctx.ellipse(0, -height * 0.335, height * 0.15, height * 0.04, 0, Math.PI, Math.PI * 2);
      ctx.fill();
    }
    ctx.restore();
  }

  function drawHouse(ctx, x, baseY, w, h, snow) {
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
    if (snow) {
      ctx.fillStyle = "rgba(249,252,253,0.96)";
      ctx.beginPath();
      ctx.moveTo(-1, roofH + 1.5);
      ctx.lineTo(w / 2, 0);
      ctx.lineTo(w + 1, roofH + 1.5);
      ctx.lineTo(w - w * 0.12, roofH + 1.5);
      ctx.lineTo(w / 2, roofH * 0.32);
      ctx.lineTo(w * 0.12, roofH + 1.5);
      ctx.closePath();
      ctx.fill();
    }
    ctx.fillStyle = "#5c3a24";
    ctx.fillRect(w * 0.4, roofH + wallH * 0.3, w * 0.2, wallH * 0.7);
    ctx.fillStyle = "#dfeaf0";
    ctx.fillRect(w * 0.14, roofH + wallH * 0.26, w * 0.17, wallH * 0.34);
    ctx.fillStyle = "#8a5f3f";
    ctx.fillRect(w * 0.52, -roofH * 0.1, w * 0.06, roofH * 0.9);
    ctx.restore();
  }

  function drawSmoke(ctx, x, y, t, h, reduced) {
    for (var i = 0; i < 3; i += 1) {
      var prog = reduced ? i / 3 : (t / 3000 + i / 3) % 1;
      var px = x + (reduced ? 0 : Math.sin(t / 800 + i * 2) * 1.4) + prog * 4;
      var py = y - prog * h * 0.34;
      var pr = 1.1 + prog * 2.1;
      ctx.fillStyle = "rgba(216,224,229," + ((1 - prog) * 0.55).toFixed(3) + ")";
      ctx.beginPath();
      ctx.arc(px, py, pr, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  function drawReeds(ctx, x, baseY, height, sway, colors) {
    for (var i = 0; i < 3; i += 1) {
      var offset = i * 2.6 - 2.6;
      ctx.strokeStyle = i === 1 ? colors[0] : colors[1];
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      ctx.moveTo(x + offset, baseY);
      ctx.quadraticCurveTo(x + offset + sway, baseY - height * 0.6, x + offset + sway * 1.6, baseY - height);
      ctx.stroke();
      ctx.fillStyle = colors[2];
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

  function drawBench(ctx, x, baseY, w, snow) {
    var seatY = baseY - 9;
    ctx.fillStyle = "#8a5f3f";
    ctx.fillRect(x, seatY, w, 2.6);
    ctx.fillStyle = "#a9793f";
    ctx.fillRect(x, seatY + 3.4, w, 1.8);
    if (snow) {
      ctx.fillStyle = "rgba(249,252,253,0.95)";
      ctx.fillRect(x, seatY - 1.3, w, 1.5);
    }
    ctx.fillStyle = "#7c5a45";
    ctx.fillRect(x + 1.5, seatY + 5, 2.2, 5.6);
    ctx.fillRect(x + w - 3.7, seatY + 5, 2.2, 5.6);
  }

  var TULIP_COLORS = ["#d9665c", "#e8b04b", "#c97bb0"];

  function drawTulip(ctx, x, y, color) {
    ctx.strokeStyle = "#5f8f68";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x, y + 3.4);
    ctx.lineTo(x, y);
    ctx.stroke();
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.moveTo(x - 1.7, y - 2.5);
    ctx.lineTo(x - 0.9, y - 1.4);
    ctx.lineTo(x, y - 2.8);
    ctx.lineTo(x + 0.9, y - 1.4);
    ctx.lineTo(x + 1.7, y - 2.5);
    ctx.quadraticCurveTo(x + 1.8, y + 0.4, x, y + 0.6);
    ctx.quadraticCurveTo(x - 1.8, y + 0.4, x - 1.7, y - 2.5);
    ctx.closePath();
    ctx.fill();
  }

  function drawBushes(ctx, w, groundY, h, drift, cfg) {
    var spots = [0.17, 0.26, 0.35, 0.63, 0.78, 0.86];
    var flowers = [0.21, 0.31, 0.66, 0.83, 0.9];
    ctx.save();
    spots.forEach(function (spot, index) {
      var x = w * spot + drift * (index % 2 ? 0.3 : 0.2);
      var radius = h * (index % 3 === 0 ? 0.13 : 0.1);
      ctx.fillStyle = index % 2 ? cfg.bushA : cfg.bushB;
      ctx.beginPath();
      ctx.arc(x, groundY + 1, radius, Math.PI, 0);
      ctx.arc(x + radius * 0.9, groundY + 1, radius * 0.72, Math.PI, 0);
      ctx.fill();
      if (cfg.blossoms && index % 2 === 0) {
        /* Весна: кусты цветут */
        for (var b = 0; b < 3; b += 1) {
          var bx = x - radius * 0.55 + b * radius * 0.55;
          var by = groundY + 1.5 - radius * (0.62 + (b % 2) * 0.28);
          ctx.fillStyle = "#f7dbe2";
          ctx.beginPath();
          ctx.arc(bx, by, 1.15, 0, Math.PI * 2);
          ctx.fill();
          ctx.fillStyle = "#eeaac1";
          ctx.beginPath();
          ctx.arc(bx, by, 0.5, 0, Math.PI * 2);
          ctx.fill();
        }
      }
    });
    flowers.forEach(function (spot, index) {
      var x = w * spot + drift * 0.25;
      var y = groundY + h * 0.16 + (index % 2) * h * 0.12;
      if (cfg.flowers === "tulip") {
        drawTulip(ctx, x, y, TULIP_COLORS[index % TULIP_COLORS.length]);
      } else if (cfg.flowers === "daisy") {
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
      }
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

  /* ── Сезонный реквизит ───────────────────────────────── */

  var LEAF_COLORS = ["#c97b3f", "#d9a441", "#a65a38", "#bb6a34"];

  function drawLeaf(ctx, x, y, angle, scale, color) {
    ctx.save();
    ctx.translate(x, y);
    ctx.rotate(angle);
    ctx.scale(scale, scale);
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.ellipse(0, 0, 2.1, 1.25, 0, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = "rgba(115,73,36,0.55)";
    ctx.lineWidth = 0.55;
    ctx.beginPath();
    ctx.moveTo(-2, 0);
    ctx.lineTo(2.9, 0);
    ctx.stroke();
    ctx.restore();
  }

  function drawFallingLeaves(ctx, t, w, groundY, reduced) {
    for (var i = 0; i < 9; i += 1) {
      var speed = 0.016 + (i % 3) * 0.006;
      var y = ((t * speed + i * 23.3) % (groundY + 10)) - 5;
      var x = ((i * 83.7) % w) + (reduced ? 0 : Math.sin(t / 940 + i * 1.7) * (4 + (i % 4)));
      x = ((x % w) + w) % w;
      drawLeaf(ctx, x, y, reduced ? i : t / 720 + i * 1.3, 0.85 + (i % 3) * 0.25, LEAF_COLORS[i % LEAF_COLORS.length]);
    }
  }

  function drawGroundLeaves(ctx, w, groundY, h, drift) {
    var spots = [0.13, 0.3, 0.42, 0.58, 0.81];
    for (var i = 0; i < spots.length; i += 1) {
      var x = w * spots[i] + drift * 0.4;
      if (x > w * 0.43 && x < w * 0.77) continue; /* не на воде */
      var y = groundY + (h - groundY) * 0.62 + (i % 2) * 2.2;
      drawLeaf(ctx, x, y, i * 1.35, 0.9, LEAF_COLORS[(i + 2) % LEAF_COLORS.length]);
    }
  }

  function drawFloatingLeaves(ctx, t, lakeLeft, lakeRight, lakeTop, groundY, reduced) {
    var span = Math.max(10, lakeRight - lakeLeft - 10);
    for (var i = 0; i < 3; i += 1) {
      var x = lakeLeft + 5 + ((t * 0.004 * (i + 1) + (i * span) / 3) % span);
      var y = lakeTop + (groundY - lakeTop) * (0.34 + (i % 2) * 0.32) + (reduced ? 0 : Math.sin(t / 800 + i) * 0.8);
      drawLeaf(ctx, x, y, reduced ? i : Math.sin(t / 900 + i * 2) * 0.4 + i, 0.8, LEAF_COLORS[(i + 1) % LEAF_COLORS.length]);
    }
  }

  function drawGeese(ctx, t, w, h, reduced) {
    var gx = reduced ? w * 0.55 : w + 90 - ((t / 55) % (w + 180));
    var gy = h * 0.2;
    var offsets = [
      [0, 0],
      [8, 4],
      [16, 8],
      [8, -4],
      [16, -8]
    ];
    ctx.strokeStyle = "rgba(96,82,63,0.85)";
    ctx.lineWidth = 1.1;
    ctx.lineCap = "round";
    for (var i = 0; i < offsets.length; i += 1) {
      var flap = reduced ? 0.8 : Math.sin(t / 240 + i * 1.1) * 2.6;
      var x = gx + offsets[i][0];
      var y = gy + offsets[i][1];
      ctx.beginPath();
      ctx.moveTo(x - 2.6, y + 0.4);
      ctx.quadraticCurveTo(x - 1.2, y - 1.4 - flap, x, y);
      ctx.quadraticCurveTo(x + 1.2, y - 1.4 - flap, x + 2.6, y + 0.4);
      ctx.stroke();
    }
    ctx.lineCap = "butt";
  }

  function drawPumpkin(ctx, x, baseY, s) {
    ctx.save();
    ctx.translate(x, baseY);
    ctx.fillStyle = "#c96f31";
    ctx.beginPath();
    ctx.ellipse(-s * 0.34, -s * 0.42, s * 0.3, s * 0.42, 0, 0, Math.PI * 2);
    ctx.fill();
    ctx.beginPath();
    ctx.ellipse(s * 0.34, -s * 0.42, s * 0.3, s * 0.42, 0, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "#e0813a";
    ctx.beginPath();
    ctx.ellipse(0, -s * 0.45, s * 0.4, s * 0.45, 0, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = "#5f7a3d";
    ctx.lineWidth = 1.1;
    ctx.beginPath();
    ctx.moveTo(0, -s * 0.86);
    ctx.quadraticCurveTo(s * 0.14, -s * 1.08, s * 0.27, -s);
    ctx.stroke();
    ctx.restore();
  }

  function drawMushrooms(ctx, w, groundY) {
    var spots = [0.232, 0.244, 0.405];
    for (var i = 0; i < spots.length; i += 1) {
      var x = w * spots[i];
      var s = i === 2 ? 0.82 : 1;
      var by = groundY + 2;
      ctx.fillStyle = "#f2e8d2";
      ctx.fillRect(x - 0.7 * s, by - 2.4 * s, 1.4 * s, 2.4 * s);
      ctx.fillStyle = "#c0523f";
      ctx.beginPath();
      ctx.arc(x, by - 2.4 * s, 1.9 * s, Math.PI, 0);
      ctx.fill();
      ctx.fillStyle = "#f8f1e2";
      ctx.beginPath();
      ctx.arc(x - 0.7 * s, by - 3 * s, 0.32 * s, 0, Math.PI * 2);
      ctx.fill();
      ctx.beginPath();
      ctx.arc(x + 0.6 * s, by - 3.2 * s, 0.3 * s, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  function drawButterfly(ctx, x, y, flap, color) {
    var wing = Math.max(0.3, Math.abs(flap));
    ctx.save();
    ctx.translate(x, y);
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.ellipse(-1.3 * wing, 0, 1.5 * wing, 1.9, -0.35, 0, Math.PI * 2);
    ctx.fill();
    ctx.beginPath();
    ctx.ellipse(1.3 * wing, 0, 1.5 * wing, 1.9, 0.35, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = "#6b5747";
    ctx.lineWidth = 0.8;
    ctx.beginPath();
    ctx.moveTo(0, -1.6);
    ctx.lineTo(0, 1.8);
    ctx.stroke();
    ctx.restore();
  }

  function drawButterflies(ctx, t, w, groundY, reduced) {
    var colors = ["#e8a4c0", "#f0b45e"];
    for (var i = 0; i < 2; i += 1) {
      var x = w * (0.3 + i * 0.24) + (reduced ? 0 : Math.sin(t / 1700 + i * 2.4) * w * 0.05);
      var y = groundY - 10 - (reduced ? 2 : Math.sin(t / 600 + i * 1.9) * 3 + 3);
      drawButterfly(ctx, x, y, reduced ? 0.9 : Math.sin(t / 110 + i * 2), colors[i]);
    }
  }

  function drawPetals(ctx, t, w, groundY, reduced) {
    var colors = ["#f8dde4", "#f3c5d1", "#fdf6ef"];
    for (var i = 0; i < 10; i += 1) {
      var y = ((t * (0.009 + (i % 3) * 0.004) + i * 27.1) % (groundY + 8)) - 4;
      var x = ((i * 71.7) % w) + (reduced ? 0 : Math.sin(t / 1250 + i * 1.9) * 6);
      x = ((x % w) + w) % w;
      ctx.save();
      ctx.translate(x, y);
      ctx.rotate(reduced ? i : t / 1500 + i * 0.9);
      ctx.fillStyle = colors[i % colors.length];
      ctx.beginPath();
      ctx.ellipse(0, 0, 1.7, 0.95, 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    }
  }

  function drawSnowfall(ctx, t, w, h, reduced) {
    for (var i = 0; i < 42; i += 1) {
      var fy = ((t * (0.013 + (i % 4) * 0.006) + i * 29.7) % (h + 8)) - 4;
      var fx = ((i * 97.13) % w) + (reduced ? 0 : Math.sin(t / (760 + (i % 5) * 140) + i) * (1.4 + (i % 3)));
      fx = ((fx % w) + w) % w;
      var r = 0.7 + (i % 3) * 0.45;
      ctx.fillStyle = "rgba(255,255,255," + (0.55 + (i % 4) * 0.11).toFixed(2) + ")";
      ctx.beginPath();
      ctx.arc(fx, fy, r, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  function drawSnowman(ctx, x, baseY, s) {
    var bodyY = baseY - s * 0.42;
    var headY = baseY - s * 0.84 - s * 0.3 + 2;
    ctx.save();
    ctx.fillStyle = "#f7fbfd";
    ctx.strokeStyle = "rgba(160,186,199,0.6)";
    ctx.lineWidth = 0.7;
    ctx.beginPath();
    ctx.arc(x, bodyY, s * 0.42, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(x, headY, s * 0.3, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    /* Руки-веточки */
    ctx.strokeStyle = "#7a5a3d";
    ctx.lineWidth = 0.9;
    ctx.beginPath();
    ctx.moveTo(x - s * 0.38, bodyY - 1);
    ctx.lineTo(x - s * 0.38 - 3.4, bodyY - 3.6);
    ctx.moveTo(x + s * 0.38, bodyY - 1);
    ctx.lineTo(x + s * 0.38 + 3.4, bodyY - 4.4);
    ctx.stroke();
    /* Шарфик */
    ctx.fillStyle = "#c45c52";
    ctx.fillRect(x - s * 0.26, headY + s * 0.22, s * 0.52, 1.7);
    ctx.fillRect(x + s * 0.12, headY + s * 0.24, 1.7, 3.4);
    /* Глаза и пуговки */
    ctx.fillStyle = "#33424d";
    ctx.beginPath();
    ctx.arc(x - s * 0.1, headY - 0.8, 0.55, 0, Math.PI * 2);
    ctx.fill();
    ctx.beginPath();
    ctx.arc(x + s * 0.1, headY - 0.8, 0.55, 0, Math.PI * 2);
    ctx.fill();
    ctx.beginPath();
    ctx.arc(x, bodyY - 1.2, 0.55, 0, Math.PI * 2);
    ctx.fill();
    ctx.beginPath();
    ctx.arc(x + 0.4, bodyY + 1.4, 0.55, 0, Math.PI * 2);
    ctx.fill();
    /* Нос-морковка */
    ctx.fillStyle = "#e0813c";
    ctx.beginPath();
    ctx.moveTo(x + 0.6, headY - 0.1);
    ctx.lineTo(x + s * 0.36, headY + 0.7);
    ctx.lineTo(x + 0.6, headY + 1.4);
    ctx.closePath();
    ctx.fill();
    ctx.restore();
  }

  function drawSparkle(ctx, x, y, r, alpha) {
    ctx.save();
    ctx.globalAlpha = Math.max(0.12, Math.min(0.85, alpha));
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 0.9;
    ctx.beginPath();
    ctx.moveTo(x - r, y);
    ctx.lineTo(x + r, y);
    ctx.moveTo(x, y - r);
    ctx.lineTo(x, y + r);
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
    var season = detectSeason(root);
    var cfg = SEASONS[season];
    root.setAttribute("data-season", season);
    root.setAttribute("aria-label", cfg.aria);
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

      drawHills(ctx, w, h, drift, cfg);

      ctx.fillStyle = cfg.sun;
      ctx.beginPath();
      ctx.arc(w * 0.9, h * 0.24, h * 0.11 + 2, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "rgba(255,255,255,0.7)";
      ctx.beginPath();
      ctx.arc(cloud - 22, h * 0.3, h * 0.07, 0, Math.PI * 2);
      ctx.arc(cloud - 12, h * 0.26, h * 0.055, 0, Math.PI * 2);
      ctx.fill();

      if (cfg.geese) drawGeese(ctx, t, w, h, reduced);

      trees.forEach(function (tree, index) {
        var sway = reduced ? 0 : Math.sin(t / 900 + tree.x * 8) * 1.3;
        var colors = cfg.trees[index % cfg.trees.length];
        drawTree(ctx, w * tree.x + drift * tree.layer, groundY, h * tree.h, sway, colors, cfg.snowTrees);
      });

      /* Озеро: вода или лёд, камыши по берегам */
      var lakeColors = cfg.frozen ? cfg.ice : cfg.water;
      var water = ctx.createLinearGradient(0, lakeTop, 0, groundY + 2);
      water.addColorStop(0, lakeColors[0]);
      water.addColorStop(1, lakeColors[1]);
      ctx.fillStyle = water;
      ctx.beginPath();
      ctx.moveTo(lakeLeft - w * 0.02, lakeTop);
      ctx.quadraticCurveTo((lakeLeft + lakeRight) / 2, lakeTop - h * 0.05, lakeRight + w * 0.02, lakeTop);
      ctx.lineTo(lakeRight + w * 0.02, groundY + 3);
      ctx.lineTo(lakeLeft - w * 0.02, groundY + 3);
      ctx.closePath();
      ctx.fill();
      if (cfg.frozen) {
        /* Снежная кромка берега, блики льда и следы коньков */
        ctx.strokeStyle = "rgba(255,255,255,0.75)";
        ctx.lineWidth = 1.3;
        ctx.beginPath();
        ctx.moveTo(lakeLeft - w * 0.02, lakeTop);
        ctx.quadraticCurveTo((lakeLeft + lakeRight) / 2, lakeTop - h * 0.05, lakeRight + w * 0.02, lakeTop);
        ctx.stroke();
        ctx.strokeStyle = "rgba(255,255,255,0.5)";
        ctx.lineWidth = 0.8;
        ctx.beginPath();
        ctx.moveTo(lakeLeft + w * 0.045, lakeTop + (groundY - lakeTop) * 0.78);
        ctx.quadraticCurveTo((lakeLeft + lakeRight) / 2, lakeTop + (groundY - lakeTop) * 0.42, lakeRight - w * 0.05, lakeTop + (groundY - lakeTop) * 0.7);
        ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(lakeLeft + w * 0.08, lakeTop + (groundY - lakeTop) * 0.55);
        ctx.quadraticCurveTo(lakeLeft + w * 0.05, lakeTop + (groundY - lakeTop) * 0.3, lakeLeft + w * 0.1, lakeTop + (groundY - lakeTop) * 0.16);
        ctx.stroke();
      } else {
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
        if (cfg.floatingLeaves) drawFloatingLeaves(ctx, t, lakeLeft, lakeRight, lakeTop, groundY, reduced);
      }
      var reedSway = reduced ? 0 : Math.sin(t / 800) * 1.6;
      var reedSway2 = reduced ? 0 : Math.sin(t / 760 + 1) * 1.6;
      drawReeds(ctx, lakeLeft - w * 0.015, groundY + 4, h * 0.34, reedSway, cfg.reeds);
      drawReeds(ctx, lakeRight + w * 0.012, groundY + 4, h * 0.3, reedSway2, cfg.reeds);
      if (cfg.duck) drawDuck(ctx, (lakeLeft + lakeRight) / 2 + w * 0.04, lakeTop + h * 0.06, reduced ? 0 : Math.sin(t / 620) * 1.6);
      if (cfg.frogs) {
        drawFrog(ctx, lakeRight + w * 0.035, groundY + 2);
        drawFrog(ctx, lakeLeft - w * 0.04, groundY + 2.4);
      }

      /* Земля: трава и тропинка */
      ctx.fillStyle = cfg.grass;
      ctx.fillRect(0, groundY, w, h - groundY);
      ctx.fillStyle = cfg.path;
      ctx.fillRect(w * 0.03, groundY + Math.max(2, (h - groundY) * 0.3), w * 0.94, Math.max(2, (h - groundY) * 0.28));

      drawBushes(ctx, w, groundY, h, drift, cfg);
      drawHouse(ctx, 5, groundY + 2, h * 0.52, h * 0.5, cfg.snowRoof);
      if (cfg.smoke) drawSmoke(ctx, 5 + h * 0.52 * 0.55, groundY + 2 - h * 0.5 - h * 0.02, t, h, reduced);
      var benchW = h * 0.6;
      drawBench(ctx, w - benchW - 6, groundY + 2, benchW, cfg.snowBench);
      if (booksImg) {
        var bookW = h * 0.42;
        var bookH = bookW * (34 / 44);
        ctx.drawImage(booksImg, w - benchW * 0.52 - bookW / 2, groundY - 9 - bookH * 0.86, bookW, bookH);
      }
      if (cfg.pumpkin) drawPumpkin(ctx, 5 + h * 0.52 + h * 0.12, groundY + 3, h * 0.17);
      if (cfg.mushrooms) drawMushrooms(ctx, w, groundY);
      if (cfg.groundLeaves) drawGroundLeaves(ctx, w, groundY, h, drift);
      if (cfg.snowman) drawSnowman(ctx, w * 0.413, groundY + 2, h * 0.23);

      /* В воздухе: листопад, лепестки, бабочки — за спиной героя */
      if (cfg.leavesFall) drawFallingLeaves(ctx, t, w, groundY, reduced);
      if (cfg.petals) drawPetals(ctx, t, w, groundY, reduced);
      if (cfg.butterflies) drawButterflies(ctx, t, w, groundY, reduced);

      var state = reduced ? { x: HOME, facing: 1, pose: "sleep", p: 1 } : sceneState(t);
      var size = Math.max(28, Math.min(h * 0.84, 46));
      var noiseX = reduced ? 0 : Math.sin(t / 180) * 1.1;
      var bounce =
        state.pose === "walk" && !reduced ? Math.abs(Math.sin(t / 95)) * 1.8 : 0;
      var petX = state.x * (w - size) + noiseX;
      var petY = h - size - (h - groundY) * 0.42 - bounce;
      var glide = state.pose === "swim" && cfg.frozen;
      if (state.pose === "swim") {
        if (glide) {
          /* Лёд: плавно выезжаем на каток и так же мягко сходим с него */
          var legK = 0.5;
          if (state.p >= 0.28 && state.p < 0.4) legK = (state.p - 0.28) / 0.12;
          else if (state.p >= 0.68 && state.p < 0.8) legK = (state.p - 0.68) / 0.12;
          var edge = Math.min(1, Math.min(legK, 1 - legK) / 0.18);
          var ease = edge * edge * (3 - 2 * edge);
          petY += (lakeTop - size + Math.max(1, h * 0.02) - petY) * ease;
        } else {
          petY += h * 0.12;
        }
      }
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
      if (glide && !reduced) ctx.rotate(Math.sin(t / 520) * 0.08);
      if (image) {
        ctx.drawImage(image, -size / 2, -size / 2, size, size);
      } else {
        ctx.fillStyle = "#98a4ae";
        ctx.beginPath();
        ctx.arc(0, 0, size / 2, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.restore();

      if (glide && !reduced) {
        /* Ледяные искры из-под коньков */
        for (var sp = 0; sp < 3; sp += 1) {
          var sx = petX + size / 2 - state.facing * (size * 0.3 + sp * 7 + ((t / 90) % 7));
          var sy = petY + size * 0.9 + (sp % 2) * 1.6;
          drawSparkle(ctx, sx, sy, 1.6 + (sp % 2) * 0.5, 0.5 + 0.3 * Math.sin(t / 230 + sp * 2.1));
        }
      }

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

      if (state.pose === "swim" && !cfg.frozen) {
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

      if (cfg.snowfall) drawSnowfall(ctx, t, w, h, reduced);

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
