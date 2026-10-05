/* Jellyfield — abyssal water column for "THE DEEP".
 * One file, two internal units:
 *   - Jellyfield2D: the shipped Canvas-2D engine, verbatim (additions: detach(),
 *     context-restore rebuild, active-slice depth sort).
 *   - selector: owns window.Jellyfield = { mount, pulse, setActivity, info }, picks
 *     the renderer (HD from jellyfield-hd.js -> 2D; reduced-motion / no-WebGL2
 *     -> 2D) and demotes HD -> 2D at runtime.
 * The WebGL renderer is the HD engine in jellyfield-hd.js (WebGL2, GLSL ES 3.00);
 * the older WebGL1 3D engine that lived here was retired with the HD bake-off
 * (spec: docs/superpowers/specs/2026-09-29-jellyfield-hd-design.md).
 * A canvas that has ever held a WebGL context can NEVER return a '2d' context, so
 * every renderer swap replaces the canvas element with a fresh clone first.
 * No libraries.
 */
(function () {
  'use strict';

  if (window.Jellyfield) { return; }

  /* ===================================================================== 2D == */
  /* The shipped Canvas-2D engine, verbatim, minus its outer IIFE + window
   * assignment + duplicate window.Jellyfield guard (the outer IIFE guards).
   * Code additions: detach() + the return of the API object, plus two
   * robustness fixes that restore the intended field — populate() sorts only
   * the active slice, and a restored 2D context rebuilds its sprites. */
  function createJellyfield2D() {
  'use strict';


  /* ---------------------------------------------------------------- palette */
  var MEDUSA = { r: 166, g: 92,  b: 199 }; /* #A65CC7 — creatures */
  var BRAND  = { r: 0,   g: 164, b: 220 }; /* #00A4DC — sonar     */
  var LUMEN  = { r: 76,  g: 242, b: 199 }; /* #4CF2C7             */
  var FOAM   = { r: 234, g: 244, b: 255 }; /* #EAF4FF             */

  function mix(a, b, t) {
    return {
      r: Math.round(a.r + (b.r - a.r) * t),
      g: Math.round(a.g + (b.g - a.g) * t),
      b: Math.round(a.b + (b.b - a.b) * t)
    };
  }
  function rgba(c, a) { return 'rgba(' + c.r + ',' + c.g + ',' + c.b + ',' + a + ')'; }
  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }
  function smoothstep(t) { return t * t * (3 - 2 * t); }

  /* ------------------------------------------------------------------ state */
  var canvas = null, ctx = null;
  var W = 0, H = 0, DPR = 1;
  var running = false, rafId = 0, lastT = 0, mounted = false;

  var reduced = false, mql = null, mqlBound = false;

  var activity = 0, activityTarget = 0;

  var mouseX = -1e5, mouseY = -1e5, mouseIn = false, lastMouseT = -1e9, cursorA = 0;

  /* --- jellyfish pool (preallocated, reused) --- */
  var MAX_JELLY = 60;
  var jellies = new Array(MAX_JELLY);
  var jellyCount = 0;

  /* --- ripple slots (max 6 concurrent, reused) --- */
  var MAX_RIPPLE = 6;
  var ripples = new Array(MAX_RIPPLE);
  var i0;
  for (i0 = 0; i0 < MAX_RIPPLE; i0++) {
    ripples[i0] = { active: false, x: 0, y: 0, r: 0, maxR: 0, speed: 0, delay: 0, strength: 0, alpha: 0 };
  }

  /* --- sonar flash --- */
  var flash = { active: false, x: 0, y: 0, t: 0, dur: 0.5 };

  /* --- prerendered sprites (built once; backdrops rebuilt on resize) --- */
  var bellSprites = [];       /* 3 color variants */
  var giantSprite = null;
  var lightSprite = null;     /* cursor light */
  var ringSprite = null;      /* ripple ring, peak at RING_PEAK of half-size */
  var flashSprite = null;
  var RING_PEAK = 0.82;
  var SPR_W = 200, SPR_H = 250, SPR_CX = 100, SPR_CY = 92; /* bell center in sprite */
  var bgCanvas = null, vigCanvas = null;

  /* ------------------------------------------------------------ bell sprite */
  function makeBellSprite(m, soft) {
    /* m: 0..1 medusa->brand mix bias; soft: near-giant variant */
    var c = document.createElement('canvas');
    c.width = SPR_W; c.height = SPR_H;
    var g = c.getContext('2d');
    var core = mix(MEDUSA, BRAND, m * 0.35);
    var rim  = mix(BRAND, MEDUSA, 0.15 + m * 0.2);
    var aMul = soft ? 0.4 : 1;
    var R = 62;

    /* outer aura */
    var aura = g.createRadialGradient(SPR_CX, SPR_CY, 6, SPR_CX, SPR_CY, soft ? 118 : 98);
    aura.addColorStop(0, rgba(core, 0.30 * aMul));
    aura.addColorStop(0.55, rgba(rim, 0.11 * aMul));
    aura.addColorStop(1, rgba(rim, 0));
    g.fillStyle = aura;
    g.fillRect(0, 0, SPR_W, SPR_H);

    /* dome path with scalloped margin (4 lobes) */
    function domePath() {
      g.beginPath();
      g.moveTo(SPR_CX - R, 108);
      g.bezierCurveTo(SPR_CX - R, 34, SPR_CX + R, 34, SPR_CX + R, 108);
      var lobes = 4, x0 = SPR_CX + R, span = 2 * R, k;
      for (k = 1; k <= lobes; k++) {
        var ex = x0 - (span * k) / lobes;
        var cxm = x0 - (span * (k - 0.5)) / lobes;
        g.quadraticCurveTo(cxm, 124, ex, 105);
      }
      g.closePath();
    }

    domePath();
    g.save();
    g.clip();
    /* bell body fill: medusa core -> brand rim */
    var body = g.createRadialGradient(SPR_CX, 80, 4, SPR_CX, 86, R * 1.18);
    body.addColorStop(0, rgba(core, 0.88 * aMul));
    body.addColorStop(0.45, rgba(mix(core, rim, 0.5), 0.5 * aMul));
    body.addColorStop(0.8, rgba(rim, 0.4 * aMul));
    body.addColorStop(1, rgba(rim, 0.06 * aMul));
    g.fillStyle = body;
    g.fillRect(0, 0, SPR_W, SPR_H);
    /* radial canals */
    g.strokeStyle = rgba(FOAM, 0.15 * aMul);
    g.lineWidth = 1.4;
    var canals = 5, ci;
    for (ci = 0; ci < canals; ci++) {
      var fx = ci / (canals - 1) - 0.5;
      g.beginPath();
      g.moveTo(SPR_CX, 56);
      g.quadraticCurveTo(SPR_CX + fx * R * 0.7, 84, SPR_CX + fx * R * 1.55, 106);
      g.stroke();
    }
    /* bright nucleus */
    var nuc = g.createRadialGradient(SPR_CX, 80, 0, SPR_CX, 82, 27);
    nuc.addColorStop(0, rgba(FOAM, 0.85 * aMul));
    nuc.addColorStop(0.4, rgba(mix(core, FOAM, 0.4), 0.4 * aMul));
    nuc.addColorStop(1, rgba(core, 0));
    g.fillStyle = nuc;
    g.fillRect(0, 0, SPR_W, SPR_H);
    /* faint bioluminescent underside at the margin */
    var und = g.createRadialGradient(SPR_CX, 108, 2, SPR_CX, 108, R * 0.9);
    und.addColorStop(0, rgba(LUMEN, 0.14 * aMul));
    und.addColorStop(1, rgba(LUMEN, 0));
    g.fillStyle = und;
    g.fillRect(0, 0, SPR_W, SPR_H);
    g.restore();

    /* rim stroke */
    domePath();
    g.strokeStyle = rgba(mix(rim, FOAM, 0.35), 0.5 * aMul);
    g.lineWidth = 2.2;
    g.stroke();
    /* crisper top-dome highlight */
    g.beginPath();
    g.moveTo(SPR_CX - R * 0.78, 78);
    g.bezierCurveTo(SPR_CX - R * 0.6, 46, SPR_CX + R * 0.6, 46, SPR_CX + R * 0.78, 78);
    g.strokeStyle = rgba(FOAM, 0.28 * aMul);
    g.lineWidth = 1.6;
    g.stroke();
    return c;
  }

  function makeRadialSprite(size, stops) {
    var c = document.createElement('canvas');
    c.width = size; c.height = size;
    var g = c.getContext('2d');
    var grad = g.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
    var i;
    for (i = 0; i < stops.length; i++) { grad.addColorStop(stops[i][0], stops[i][1]); }
    g.fillStyle = grad;
    g.fillRect(0, 0, size, size);
    return c;
  }

  function buildSprites() {
    bellSprites[0] = makeBellSprite(0.0, false);
    bellSprites[1] = makeBellSprite(0.5, false);
    bellSprites[2] = makeBellSprite(1.0, false);
    giantSprite = makeBellSprite(0.3, true);
    lightSprite = makeRadialSprite(256, [
      [0, rgba(mix(BRAND, FOAM, 0.55), 0.22)],
      [0.45, rgba(mix(BRAND, FOAM, 0.2), 0.09)],
      [1, rgba(BRAND, 0)]
    ]);
    flashSprite = makeRadialSprite(256, [
      [0, rgba(mix(BRAND, FOAM, 0.7), 0.9)],
      [0.35, rgba(BRAND, 0.35)],
      [1, rgba(BRAND, 0)]
    ]);
    /* sonar ring: soft-edged annulus with its peak at RING_PEAK */
    var c = document.createElement('canvas');
    c.width = 512; c.height = 512;
    var g = c.getContext('2d');
    var grad = g.createRadialGradient(256, 256, 0, 256, 256, 256);
    grad.addColorStop(0, rgba(BRAND, 0));
    grad.addColorStop(RING_PEAK - 0.16, rgba(BRAND, 0));
    grad.addColorStop(RING_PEAK - 0.05, rgba(BRAND, 0.28));
    grad.addColorStop(RING_PEAK, rgba(mix(BRAND, FOAM, 0.65), 0.85));
    grad.addColorStop(RING_PEAK + 0.05, rgba(BRAND, 0.25));
    grad.addColorStop(clamp(RING_PEAK + 0.14, 0, 1), rgba(BRAND, 0));
    grad.addColorStop(1, rgba(BRAND, 0));
    g.fillStyle = grad;
    g.fillRect(0, 0, 512, 512);
    ringSprite = c;
  }

  /* -------------------------------------------------- background / vignette */
  function buildBackdrops() {
    /* water column gradient + light shaft (under everything) */
    bgCanvas = document.createElement('canvas');
    bgCanvas.width = Math.max(1, Math.round(W * DPR));
    bgCanvas.height = Math.max(1, Math.round(H * DPR));
    var g = bgCanvas.getContext('2d');
    g.setTransform(DPR, 0, 0, DPR, 0, 0);
    var grad = g.createLinearGradient(0, 0, 0, H);
    grad.addColorStop(0, '#0B2036');
    grad.addColorStop(0.34, '#08182B');
    grad.addColorStop(1, '#040A12');
    g.fillStyle = grad;
    g.fillRect(0, 0, W, H);
    /* faint light shaft falling from the surface */
    var sx = W * 0.6, i;
    for (i = 0; i < 5; i++) {
      var half = W * (0.045 + i * 0.05);
      var sg = g.createLinearGradient(0, 0, 0, H * 0.74);
      sg.addColorStop(0, 'rgba(130,185,235,' + (0.05 - i * 0.009).toFixed(3) + ')');
      sg.addColorStop(1, 'rgba(130,185,235,0)');
      g.fillStyle = sg;
      g.beginPath();
      g.moveTo(sx - half * 0.45, -4);
      g.lineTo(sx + half * 0.45, -4);
      g.lineTo(sx + half * 1.7, H * 0.74);
      g.lineTo(sx - half * 1.7, H * 0.74);
      g.closePath();
      g.fill();
    }

    /* vignette (over everything): darker at edges */
    vigCanvas = document.createElement('canvas');
    vigCanvas.width = bgCanvas.width;
    vigCanvas.height = bgCanvas.height;
    var v = vigCanvas.getContext('2d');
    v.setTransform(DPR, 0, 0, DPR, 0, 0);
    var m = Math.max(W, H);
    var vg = v.createRadialGradient(W * 0.5, H * 0.42, Math.min(W, H) * 0.3, W * 0.5, H * 0.5, m * 0.78);
    vg.addColorStop(0, 'rgba(2,5,10,0)');
    vg.addColorStop(0.7, 'rgba(2,5,10,0.22)');
    vg.addColorStop(1, 'rgba(2,5,10,0.6)');
    v.fillStyle = vg;
    v.fillRect(0, 0, W, H);
  }

  /* -------------------------------------------------------------- jellyfish */
  function makeJelly() {
    return {
      x: 0, y: 0, depth: 0, scale: 1, baseAlpha: 0.6,
      speed: 0, kick: 0,
      phase: 0, phaseRate: 1,
      wobPhase: 0, wobRate: 1, wobAmp: 8,
      vx: 0, vy: 0,               /* transient impulses (ripples) */
      ox: 0, oy: 0,               /* eased flow-field offset */
      tox: 0, toy: 0,             /* flow-field target (scratch) */
      bright: 1, brightT: 1,
      seed: 0, sprite: 0, giant: false,
      tentN: 5, tentLen: 60,
      tentColor: '', armColor: ''
    };
  }
  for (i0 = 0; i0 < MAX_JELLY; i0++) { jellies[i0] = makeJelly(); }

  function initJelly(j, giant) {
    var d = giant ? (0.96 + Math.random() * 0.04) : Math.random();
    j.depth = d;
    j.giant = giant;
    j.scale = giant ? (1.6 + Math.random() * 0.5) : (0.35 + d * 0.75); /* 0.35..1.1 */
    j.baseAlpha = giant ? 0.16 : (0.34 + d * 0.56);
    j.x = Math.random() * W;
    j.y = Math.random() * H;
    j.speed = (7 + Math.random() * 9) * (0.45 + d * 0.75) * (giant ? 0.5 : 1);
    j.kick = (16 + Math.random() * 14) * (0.5 + d * 0.6);
    j.phase = Math.random() * Math.PI * 2;
    j.phaseRate = (0.9 + Math.random() * 0.7) * (giant ? 0.5 : 1);
    j.wobPhase = Math.random() * Math.PI * 2;
    j.wobRate = 0.25 + Math.random() * 0.35;
    j.wobAmp = (5 + Math.random() * 9) * (0.5 + d);
    j.vx = 0; j.vy = 0; j.ox = 0; j.oy = 0; j.bright = 1; j.brightT = 1;
    j.seed = Math.random() * Math.PI * 2;
    j.sprite = (Math.random() * 3) | 0;
    j.tentN = 4 + ((Math.random() * 3) | 0);      /* 4..6 */
    j.tentLen = 68 + Math.random() * 52;
    var tm = Math.random();
    j.tentColor = rgba(mix(MEDUSA, BRAND, tm * 0.5), 0.62);
    j.armColor = rgba(mix(MEDUSA, FOAM, 0.25), 0.42);
  }

  function populate() {
    var mobile = W <= 720;
    var n = mobile ? 25 : 48;
    var giants = mobile ? 1 : 2;
    jellyCount = Math.min(MAX_JELLY, n + giants);
    var i;
    for (i = 0; i < jellyCount; i++) {
      initJelly(jellies[i], i >= jellyCount - giants);
    }
    /* depth sort once: far first, giants (nearest) last; depth never changes.
       Sort ONLY the active slice: sorting the whole 60-slot pool pulled the
       idle slots (depth 0 — placeholders parked at the top-left corner, or
       stale leftovers from a previous density) into [0, jellyCount) and
       pushed the deepest real jellies, the giants always among them, past
       jellyCount where they are never drawn. On a phone the 34 idle slots
       outnumber the 26 real ones, so NO real jelly was drawn at all. */
    var active = jellies.slice(0, jellyCount);
    active.sort(function (a, b) { return a.depth - b.depth; });
    for (i = 0; i < jellyCount; i++) { jellies[i] = active[i]; }
  }

  /* ----------------------------------------------------------------- update */
  function update(dt, now) {
    activity += (activityTarget - activity) * Math.min(1, dt * 1.6);
    if (Math.abs(activityTarget - activity) < 0.002) { activity = activityTarget; }

    /* cursor light presence (fade after 2s idle or pointer gone) */
    var wantCursor = mouseIn && (now - lastMouseT < 2000);
    cursorA += ((wantCursor ? 1 : 0) - cursorA) * Math.min(1, dt * 4);

    /* ripples */
    var ri;
    for (ri = 0; ri < MAX_RIPPLE; ri++) {
      var rp = ripples[ri];
      if (!rp.active) { continue; }
      if (rp.delay > 0) { rp.delay -= dt; continue; }
      rp.r += rp.speed * dt;
      var f = 1 - rp.r / rp.maxR;
      if (f <= 0) { rp.active = false; continue; }
      rp.alpha = f * f * Math.min(1, rp.strength);
    }
    if (flash.active) {
      flash.t += dt;
      if (flash.t >= flash.dur) { flash.active = false; }
    }

    var speedMul = 0.55 + activity * 1.5;
    var i;
    for (i = 0; i < jellyCount; i++) {
      var j = jellies[i];
      j.phase += j.phaseRate * (0.65 + activity * 0.9) * dt;
      j.wobPhase += j.wobRate * dt;

      /* bell contraction: sharp on the positive half -> propulsion kick */
      var s = Math.sin(j.phase);
      var c = s > 0 ? s * s : 0;

      /* transient impulses decay */
      var decay = Math.max(0, 1 - dt * 2.4);
      j.vx *= decay; j.vy *= decay;

      j.x += (Math.sin(j.wobPhase) * j.wobAmp * 0.55 + j.vx) * dt;
      j.y += (-(j.speed + j.kick * c) * speedMul + j.vy) * dt;

      /* wrap: rises past the surface -> respawn below */
      var mrg = 170 * j.scale;
      if (j.y < -mrg) { j.y = H + mrg * 0.8; j.x = Math.random() * W; }
      else if (j.y > H + mrg * 1.5) { j.y = -mrg * 0.5; }
      if (j.x < -mrg) { j.x = W + mrg; } else if (j.x > W + mrg) { j.x = -mrg; }

      /* flow field: part around cursor, smoothstep falloff, depth-scaled */
      j.tox = 0; j.toy = 0;
      var bT = 1;
      if (mouseIn) {
        var dx = j.x - mouseX, dy = j.y - mouseY;
        var R = 180 * (0.45 + j.depth * 0.75);
        var d2 = dx * dx + dy * dy;
        if (d2 < R * R && d2 > 0.01) {           /* early-out for far jellies */
          var d = Math.sqrt(d2);
          var t = smoothstep(1 - d / R);
          var push = (62 * (0.4 + j.depth)) * t;
          j.tox = (dx / d) * push;
          j.toy = (dy / d) * push;
          bT = 1 + 0.5 * t;                       /* brighten up to 1.5x */
        }
      }
      j.ox += (j.tox - j.ox) * Math.min(1, dt * 4.2);
      j.oy += (j.toy - j.oy) * Math.min(1, dt * 4.2);

      /* ripple wavefronts push + flash as they pass */
      for (ri = 0; ri < MAX_RIPPLE; ri++) {
        var rp2 = ripples[ri];
        if (!rp2.active || rp2.delay > 0) { continue; }
        var rdx = j.x - rp2.x, rdy = j.y - rp2.y;
        var band = 70;
        var lo = rp2.r - band, hi = rp2.r + band;
        var rd2 = rdx * rdx + rdy * rdy;
        if (rd2 > hi * hi || (lo > 0 && rd2 < lo * lo)) { continue; }
        var rd = Math.sqrt(rd2);
        if (rd < 0.5) { continue; }
        var w = 1 - Math.abs(rd - rp2.r) / band;
        w = w * w * rp2.alpha;
        j.vx += (rdx / rd) * w * 340 * dt * (0.4 + j.depth);
        j.vy += (rdy / rd) * w * 340 * dt * (0.4 + j.depth);
        var rb = 1 + w * 1.1;
        if (rb > bT) { bT = rb; }
      }

      j.brightT = bT;
      j.bright += (j.brightT - j.bright) * Math.min(1, dt * 3);
    }
  }

  /* ------------------------------------------------------------------- draw */
  function drawJelly(j, dim) {
    var s = Math.sin(j.phase);
    var c = s > 0 ? s * s : 0;
    var sqX = 1 - 0.26 * c;   /* bell narrows at contraction */
    var sqY = 1 + 0.17 * c;   /* ...and elongates */
    var sc = j.scale * 0.55;  /* sprite unit -> css px */
    var alpha = j.baseAlpha * (0.8 + 0.3 * activity) * Math.min(1.35, j.bright) * dim;
    if (alpha <= 0.01) { return; }

    ctx.save();
    ctx.translate(j.x + j.ox, j.y + j.oy);

    /* tentacles behind the bell */
    var n = j.tentN, i;
    ctx.lineCap = 'round';
    ctx.strokeStyle = j.tentColor;
    ctx.lineWidth = Math.max(0.6, 1.7 * sc);
    ctx.globalAlpha = alpha * 0.8;
    var lean = -Math.sin(j.wobPhase) * 9 * sc;
    for (i = 0; i < n; i++) {
      var fx = n > 1 ? (i / (n - 1) - 0.5) : 0;
      var ax = fx * 96 * sc * sqX;
      var ay = 15 * sc * sqY;
      var len = j.tentLen * sc * (1 - 0.16 * c) * (1 - Math.abs(fx) * 0.5);
      var sw1 = Math.sin(j.phase * 0.85 + j.seed + i * 1.9) * 15 * sc;
      var sw2 = Math.sin(j.phase * 0.85 + j.seed + i * 1.9 + 1.45) * 24 * sc;
      ctx.beginPath();
      ctx.moveTo(ax, ay);
      ctx.quadraticCurveTo(ax + sw1 * 0.55 + lean * 0.3, ay + len * 0.5, ax + sw1 + lean * 0.6, ay + len * 0.82);
      ctx.quadraticCurveTo(ax + sw1 + (sw2 - sw1) * 0.7 + lean * 0.8, ay + len * 0.95, ax + sw2 + lean, ay + len);
      ctx.stroke();
    }
    /* two thicker oral arms */
    ctx.strokeStyle = j.armColor;
    ctx.lineWidth = Math.max(1, 3.1 * sc);
    ctx.globalAlpha = alpha * 0.6;
    for (i = 0; i < 2; i++) {
      var side = i === 0 ? -1 : 1;
      var ax2 = side * 14 * sc * sqX;
      var len2 = j.tentLen * sc * 0.62 * (1 - 0.16 * c);
      var sw = Math.sin(j.phase * 0.85 + j.seed + 3 + i * 2.4) * 12 * sc;
      ctx.beginPath();
      ctx.moveTo(ax2, 12 * sc * sqY);
      ctx.quadraticCurveTo(ax2 + sw * 0.6 + lean * 0.4, len2 * 0.55, ax2 + sw + lean * 0.8, len2);
      ctx.stroke();
    }

    /* bell */
    var spr = j.giant ? giantSprite : bellSprites[j.sprite];
    var bw = SPR_W * sc * sqX;
    var bh = SPR_H * sc * sqY;
    ctx.globalAlpha = alpha;
    ctx.drawImage(spr, -SPR_CX * sc * sqX, -SPR_CY * sc * sqY, bw, bh);
    /* excitement glow pass */
    if (j.bright > 1.06) {
      ctx.globalAlpha = Math.min(0.7, (j.bright - 1) * 0.75) * dim;
      ctx.drawImage(spr, -SPR_CX * sc * sqX * 1.06, -SPR_CY * sc * sqY * 1.06, bw * 1.06, bh * 1.06);
    }
    ctx.restore();
  }

  function draw() {
    /* water */
    ctx.globalCompositeOperation = 'source-over';
    ctx.globalAlpha = 1;
    ctx.drawImage(bgCanvas, 0, 0, W, H);

    /* luminous passes */
    ctx.globalCompositeOperation = 'lighter';

    /* cursor light illuminates the water locally */
    if (cursorA > 0.01) {
      var lr = 300 + activity * 90;
      ctx.globalAlpha = cursorA * 0.9;
      ctx.drawImage(lightSprite, mouseX - lr, mouseY - lr, lr * 2, lr * 2);
    }

    /* jellyfish, far -> near (pre-sorted by depth); dimmed under the text column */
    var i;
    for (i = 0; i < jellyCount; i++) {
      var jd = jellies[i];
      drawJelly(jd, columnDim(jd.x + jd.ox));
    }

    /* ripples */
    for (i = 0; i < MAX_RIPPLE; i++) {
      var rp = ripples[i];
      if (!rp.active || rp.delay > 0 || rp.r <= 1) { continue; }
      var dsz = (rp.r / RING_PEAK) * 2;
      ctx.globalAlpha = rp.alpha * 0.9;
      ctx.drawImage(ringSprite, rp.x - dsz / 2, rp.y - dsz / 2, dsz, dsz);
    }

    /* sonar flash at the pulse origin */
    if (flash.active) {
      var ft = 1 - flash.t / flash.dur;
      var fr = 120 + (1 - ft) * 160;
      ctx.globalAlpha = ft * ft * 0.85;
      ctx.drawImage(flashSprite, flash.x - fr, flash.y - fr, fr * 2, fr * 2);
    }

    /* field-energy shimmer: whole water brightens with activity */
    if (activity > 0.02) {
      ctx.globalAlpha = activity * 0.06;
      ctx.fillStyle = '#7FD8FF';
      ctx.fillRect(0, 0, W, H);
    }

    /* reset compositing, then vignette on top */
    ctx.globalCompositeOperation = 'source-over';
    ctx.globalAlpha = 1;
    ctx.drawImage(vigCanvas, 0, 0, W, H);
  }

  /* --------------------------------------------------------- reduced motion */
  function renderStatic() {
    if (!ctx) { return; }
    ctx.globalCompositeOperation = 'source-over';
    ctx.globalAlpha = 1;
    ctx.drawImage(bgCanvas, 0, 0, W, H);
    ctx.globalCompositeOperation = 'lighter';
    var n = Math.min(8, jellyCount), i;
    for (i = 0; i < n; i++) {
      var j = jellies[Math.min(jellyCount - 1, Math.floor((i + 0.5) * jellyCount / n))];
      /* deterministic calm placement, dim, near-still */
      j.x = W * (0.1 + 0.8 * ((i * 0.618034 + 0.07) % 1));
      j.y = H * (0.12 + 0.74 * ((i * 0.381966 + 0.23) % 1));
      j.ox = 0; j.oy = 0; j.bright = 1; j.phase = 2.2 + i * 0.9;
      drawJelly(j, 0.55 * columnDim(j.x));
    }
    ctx.globalCompositeOperation = 'source-over';
    ctx.globalAlpha = 1;
    ctx.drawImage(vigCanvas, 0, 0, W, H);
  }

  /* ------------------------------------------------------------------- loop */
  function frame(t) {
    rafId = 0;
    if (!running) { return; }
    var dt = (t - lastT) / 1000;
    lastT = t;
    if (dt > 0.05) { dt = 0.05; }
    if (dt > 0) { update(dt, t); }
    draw();
    rafId = requestAnimationFrame(frame);
  }

  function start() {
    if (running || reduced || !mounted) { return; }
    running = true;
    lastT = performance.now();
    if (!rafId) { rafId = requestAnimationFrame(frame); }
  }

  function stop() {
    running = false;
    if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
  }

  /* ----------------------------------------------------------- mgmt/events */

  /* Text-column dim band (a11y): the page's overlay text (tick labels,
     eyebrows) sits directly on the water. Jellies passing under it are dimmed
     toward DIM_FLOOR so small benthos text keeps a dark backdrop. */
  var dimL = 0, dimR = -1, DIM_FEATHER = 120, DIM_FLOOR = 0.35;
  function measureColumn() {
    var col = document.querySelector('.column');
    if (col) {
      var r = col.getBoundingClientRect();
      dimL = r.left - 60;   /* also cover rail marks hanging left of the column */
      dimR = r.right;
    } else {
      dimL = 0; dimR = -1;  /* disabled */
    }
  }
  function columnDim(x) {
    if (dimR < dimL) { return 1; }
    var t;
    if (x >= dimL && x <= dimR) { t = 0; }
    else if (x < dimL) { t = (dimL - x) / DIM_FEATHER; }
    else { t = (x - dimR) / DIM_FEATHER; }
    if (t >= 1) { return 1; }
    return DIM_FLOOR + (1 - DIM_FLOOR) * smoothstep(t);
  }

  function applyResize() {
    if (!canvas) { return; }
    var rect = canvas.getBoundingClientRect();
    var newW = rect.width || window.innerWidth;
    var newH = rect.height || window.innerHeight;
    var newDPR = Math.min(2, window.devicePixelRatio || 1);
    measureColumn();
    if (newW === W && newH === H && newDPR === DPR && bgCanvas) { return; }
    var oldW = W, oldH = H;
    W = newW; H = newH; DPR = newDPR;
    canvas.width = Math.max(1, Math.round(W * DPR));
    canvas.height = Math.max(1, Math.round(H * DPR));
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    buildBackdrops();
    var wasMobile = oldW > 0 && oldW <= 720;
    var isMobile = W <= 720;
    if (jellyCount === 0 || oldW <= 0 || oldH <= 0 || wasMobile !== isMobile) {
      populate();               /* first mount, or the density breakpoint flipped */
    } else {
      /* rescale existing positions so the field survives the resize
         instead of teleporting to fresh random spots mid-gesture */
      var sx = W / oldW, sy = H / oldH, i;
      for (i = 0; i < jellyCount; i++) { jellies[i].x *= sx; jellies[i].y *= sy; }
    }
    if (reduced) { renderStatic(); }
  }

  /* 2D context loss (GPU reset, driver update, a backgrounded tab under
     memory pressure). The spec restores a 2D context BY ITSELF unless
     'contextlost' is cancelled — which is why that event is deliberately not
     listened to — but it comes back blank with its state reset: identity
     transform (quarter-scale on DPR>1) and, since draw() never clears and
     leans on the opaque backdrop, 'lighter' strokes smearing forever. The
     sprite and backdrop canvases are lost in the same reset, so rebuild all
     of it in place. W/H are deliberately NOT zeroed: that re-runs populate()
     and teleports the whole field; a null bgCanvas alone defeats
     applyResize's unchanged-size early return and rescales positions by 1. */
  function onContextRestored() {
    if (!mounted || !ctx) { return; }
    buildSprites();
    bgCanvas = null;
    applyResize();
  }

  /* Debounced 150ms trailing: drag-resize and mobile URL-bar/soft-keyboard
     viewport changes fire resize per frame, and each applyResize reallocates
     two full-screen DPR-scaled canvases — run it once per settled size. */
  var resizeTimer = 0;
  function resize() {
    if (resizeTimer) { clearTimeout(resizeTimer); }
    resizeTimer = setTimeout(function () { resizeTimer = 0; applyResize(); }, 150);
  }

  function onMouseMove(e) {
    if (reduced) { return; }
    mouseX = e.clientX;
    mouseY = e.clientY;
    mouseIn = true;
    lastMouseT = performance.now();
  }

  function onMouseOut(e) {
    if (!e.relatedTarget) { mouseIn = false; }
  }

  function onVisibility() {
    if (document.hidden) { stop(); }
    else if (!reduced) { start(); }
  }

  function onMotionChange() {
    reduced = !!(mql && mql.matches);
    if (reduced) {
      stop();
      activity = 0; activityTarget = 0; cursorA = 0; mouseIn = false;
      var i;
      for (i = 0; i < MAX_RIPPLE; i++) { ripples[i].active = false; }
      flash.active = false;
      renderStatic();
    } else {
      populate();
      start();
    }
  }

  /* --------------------------------------------------------------- API ---- */
  function mount(canvasEl) {
    if (!canvasEl || typeof canvasEl.getContext !== 'function') { return; }
    if (mounted && canvasEl === canvas) { return; }
    if (mounted) { stop(); }
    if (canvas && canvas !== canvasEl) { canvas.removeEventListener('contextrestored', onContextRestored); }
    canvas = canvasEl;
    ctx = canvas.getContext('2d');
    if (!ctx) { return; }
    canvas.addEventListener('contextrestored', onContextRestored);
    if (!bellSprites[0]) { buildSprites(); }

    mql = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
    reduced = !!(mql && mql.matches);
    if (mql && !mqlBound) {
      mqlBound = true;
      if (typeof mql.addEventListener === 'function') { mql.addEventListener('change', onMotionChange); }
      else if (typeof mql.addListener === 'function') { mql.addListener(onMotionChange); }
    }

    if (!mounted) {
      window.addEventListener('mousemove', onMouseMove, { passive: true });
      window.addEventListener('mouseout', onMouseOut, { passive: true });
      /* NOTE: no engine-internal click listener — the page owns click->pulse
         wiring (it excludes controls that fire their own bloom), so each click
         spawns exactly one ripple. */
      window.addEventListener('resize', resize);
      document.addEventListener('visibilitychange', onVisibility);
    }
    mounted = true;

    applyResize();
    if (reduced) { renderStatic(); }
    else { start(); }
  }

  function pulse(clientX, clientY, strength) {
    if (!mounted || reduced) { return; }
    var s = (typeof strength === 'number' && isFinite(strength)) ? strength : 1;
    var x = typeof clientX === 'number' ? clientX : W * 0.5;
    var y = typeof clientY === 'number' ? clientY : H * 0.5;
    var rings = s >= 2 ? 3 : 1;
    var k;
    for (k = 0; k < rings; k++) {
      /* claim a free slot, else recycle the most-expired ripple (cap 6) */
      var slot = null, oldest = null, oldestF = -1, ri;
      for (ri = 0; ri < MAX_RIPPLE; ri++) {
        var rp = ripples[ri];
        if (!rp.active) { slot = rp; break; }
        var fexp = rp.maxR > 0 ? rp.r / rp.maxR : 1;
        if (fexp > oldestF) { oldestF = fexp; oldest = rp; }
      }
      if (!slot) { slot = oldest; }
      slot.active = true;
      slot.x = x;
      slot.y = y;
      slot.r = 0;
      slot.maxR = (rings > 1 ? Math.max(W, H) * 0.85 : 430) * (0.75 + Math.min(s, 3) * 0.18);
      slot.speed = (rings > 1 ? 560 : 430) * (0.8 + Math.min(s, 3) * 0.12);
      slot.delay = k * 0.18;                       /* staggered ~180ms */
      slot.strength = Math.min(s, 3) * (1 - k * 0.22);
      slot.alpha = 0;
    }
    if (s >= 2) {
      flash.active = true;
      flash.x = x;
      flash.y = y;
      flash.t = 0;
    }
  }

  function setActivity(level) {
    var v = (typeof level === 'number' && isFinite(level)) ? level : 0;
    activityTarget = clamp(v, 0, 1);
  }

  /* detach(): removes the engine's window/document/canvas listeners so the
     selector can swap renderers cleanly. */
  function detach() {
    stop();
    if (resizeTimer) { clearTimeout(resizeTimer); resizeTimer = 0; }
    if (mounted) {
      window.removeEventListener('mousemove', onMouseMove);
      window.removeEventListener('mouseout', onMouseOut);
      window.removeEventListener('resize', resize);
      document.removeEventListener('visibilitychange', onVisibility);
    }
    if (mql && mqlBound) {
      mqlBound = false;
      if (typeof mql.removeEventListener === 'function') { mql.removeEventListener('change', onMotionChange); }
      else if (typeof mql.removeListener === 'function') { mql.removeListener(onMotionChange); }
      mql = null;
    }
    if (canvas) { canvas.removeEventListener('contextrestored', onContextRestored); }
    mounted = false;
    canvas = null;
    ctx = null;
    /* Bust the cached sizing so a remount on a swapped-in fresh canvas
       re-runs the full applyResize (canvas.width/height + setTransform DPR)
       instead of early-returning on unchanged W/H/DPR — otherwise the new
       identity-transform context draws quarter-scale on DPR>1 displays. */
    W = 0; H = 0;
    bgCanvas = null;
  }

  return { mount: mount, pulse: pulse, setActivity: setActivity, detach: detach };
  }

  /* =============================================================== selector == */
  /* Owns window.Jellyfield. Picks the renderer at mount (HD -> 2D; HD is the
   * engine registered by jellyfield-hd.js, which must load before this file),
   * re-selects when prefers-reduced-motion flips (both directions), and
   * demotes to 2D when the GL context dies for good. A canvas only ever
   * yields ONE context kind, so every swap replaces the canvas element with a
   * fresh clone first. */
  var sel2d = null;
  var selHD = null;
  var selActive = null;
  var selKind = '';
  var selCanvas = null;
  var selMql = null;
  var selLastActivity = 0;

  function selFreshCanvas() {
    if (!selCanvas) { return null; }
    var parent = selCanvas.parentNode;
    if (!parent) { return selCanvas; }   /* best effort: cannot swap a detached node */
    var fresh = selCanvas.cloneNode(false);   /* keeps id/class/attrs */
    parent.replaceChild(fresh, selCanvas);
    selCanvas = fresh;
    return fresh;
  }

  function selUse2D() {
    if (!sel2d) { sel2d = createJellyfield2D(); }
    selActive = sel2d;
    selKind = '2d';
    sel2d.mount(selCanvas);
    sel2d.setActivity(selLastActivity);
  }

  function selTryHD() {
    var HD = window.JellyfieldHD;
    if (!HD) { return false; }
    if (!selHD) { selHD = HD.create(); selHD.onFatal = selOnFatalHD; }
    if (selHD.mount(selCanvas)) {
      selActive = selHD;
      selKind = 'hd';
      selHD.setActivity(selLastActivity);
      return true;
    }
    return false;
  }

  function selOnFatalHD() {
    /* the GPU context died and could not be restored: 2D for the session. The
       old canvas is locked to its dead WebGL context — swap in a fresh one. */
    if (selKind !== 'hd') { return; }
    selHD.detach();
    selFreshCanvas();
    selUse2D();
  }

  function selReduced() {
    return !!(selMql && selMql.matches);
  }

  function selOnMotionChange() {
    if (!selCanvas || !selKind) { return; }
    if (selReduced() && selKind === 'hd') {
      /* HD has no static mode: hand the field to the 2D renderer. */
      selActive.detach();
      selFreshCanvas();
      selUse2D();
    } else if (!selReduced() && selKind === '2d') {
      /* motion allowed again: try to promote back to HD. */
      sel2d.detach();
      selFreshCanvas();
      if (selTryHD()) { return; }
      selFreshCanvas();   /* the failed HD attempt may have claimed a GL context */
      selUse2D();
    }
    /* reduced with 2D active: the 2D engine renders its own static field. */
  }

  function selMount(canvasEl) {
    if (!canvasEl || typeof canvasEl.getContext !== 'function') { return; }
    if (selCanvas && selActive) { return; }   /* one mount per page */
    selCanvas = canvasEl;
    if (!selMql && window.matchMedia) {
      selMql = window.matchMedia('(prefers-reduced-motion: reduce)');
      if (selMql) {
        if (typeof selMql.addEventListener === 'function') { selMql.addEventListener('change', selOnMotionChange); }
        else if (typeof selMql.addListener === 'function') { selMql.addListener(selOnMotionChange); }
      }
    }
    if (selReduced()) { selUse2D(); return; }
    if (selTryHD()) { return; }
    selFreshCanvas();   /* the failed HD attempt may have claimed a GL context */
    selUse2D();
  }

  function selPulse(clientX, clientY, strength) {
    if (selActive) { selActive.pulse(clientX, clientY, strength); }
  }

  function selSetActivity(level) {
    var v = (typeof level === 'number' && isFinite(level)) ? level : 0;
    selLastActivity = v < 0 ? 0 : (v > 1 ? 1 : v);
    if (selActive) { selActive.setActivity(level); }
  }

  function selInfo() {
    /* read-only, for tests and tooling: the active engine's own info() (only
       HD has one) over a fixed set of null fields, so callers can rely on
       every key being present whichever engine won */
    var base = (selActive && typeof selActive.info === 'function') ? selActive.info() : {};
    var out = { engine: selKind, species: null, tier: null, dpr: null, drawW: null, drawH: null,
                renderScale: null, hdr: null, frames: null, heroBox: null };
    var k;
    for (k in base) { if (Object.prototype.hasOwnProperty.call(base, k)) { out[k] = base[k]; } }
    out.engine = selKind;
    return out;
  }

  window.Jellyfield = { mount: selMount, pulse: selPulse, setActivity: selSetActivity, info: selInfo };
})();
