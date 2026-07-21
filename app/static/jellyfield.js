/* Jellyfield — abyssal water column for "THE DEEP".
 * One file, three internal units:
 *   - Jellyfield2D: the shipped Canvas-2D engine, verbatim (only addition: detach()).
 *   - Jellyfield3D: WebGL water column — instanced shader jellyfish in real depth,
 *     fog, light shafts, camera parallax, ray flow field, 3D shockwave pulses.
 *   - selector: owns window.Jellyfield = { mount, pulse, setActivity }, picks the
 *     renderer (reduced-motion / no-WebGL -> 2D) and demotes 3D -> 2D at runtime.
 * A canvas that has ever held a WebGL context can NEVER return a '2d' context, so
 * every renderer swap replaces the canvas element with a fresh clone first.
 * No libraries. GLSL ES 1.00 (works on webgl2 and webgl1 + ANGLE_instanced_arrays).
 */
(function () {
  'use strict';

  if (window.Jellyfield) { return; }

  /* ===================================================================== 2D == */
  /* The shipped Canvas-2D engine, verbatim, minus its outer IIFE + window
   * assignment + duplicate window.Jellyfield guard (the outer IIFE guards).
   * The ONLY code addition is detach() + the return of the API object. */
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
    /* depth sort once: far first, giants (nearest) last; depth never changes */
    jellies.sort(function (a, b) { return a.depth - b.depth; });
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
    canvas = canvasEl;
    ctx = canvas.getContext('2d');
    if (!ctx) { return; }
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

  /* detach(): the ONLY addition to the shipped 2D engine — removes its
     window/document listeners so the selector can swap renderers cleanly. */
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

  /* ===================================================================== 3D == */
  function createJellyfield3D() {
    'use strict';

    /* ------------------------------------------------------------ constants */
    var MAX_SHELL = 6;
    var FOVY = 55 * Math.PI / 180;
    var NEARZ = 0.5, FARZ = 80;
    var FOG_D = 0.048;               /* exponential fog toward the abyss */
    var MAXTILT = 3 * Math.PI / 180; /* mouse parallax: +-3 degrees */
    var PULSE_Z = -13;               /* shockwaves detonate at mid-depth */
    var CURSOR_Z = -10;              /* cursor light lives at mid-depth */
    var BELL_SEG = 96, BELL_RINGS = 48;   /* super-res hero bell */
    var HERO_Z = -8.2;               /* the Medusa's depth */
    var N_SIL = 6;                   /* distant silhouettes */
    var CH_TENT = 32, TENT_NODES = 24;    /* marginal tentacles (verlet) */
    var CH_ARM = 4, ARM_NODES = 16;       /* frilled oral arms (verlet) */
    var CHAINS = CH_TENT + CH_ARM;
    var TOTAL_NODES = CH_TENT * TENT_NODES + CH_ARM * ARM_NODES;  /* 832 */
    var TENT_LEN = 1.55, ARM_LEN = 1.05;  /* in bell-local units */
    var BREATH = 4.5;                /* seconds per contraction cycle */
    var TWO_PI = 6.283185307179586;

    /* --------------------------------------------------------------- state */
    var canvas = null, gl = null, isGL2 = false, instExt = null;
    var W = 0, H = 0, DPR = 1, aspect = 1;
    var tanY = Math.tan(FOVY / 2), tanXA = tanY;
    var running = false, rafId = 0, lastT = 0, mounted = false, dead = false;
    var contextLost = false;
    var timeS = 0;

    var activity = 0, activityTarget = 0;

    var mouseNX = 0, mouseNY = 0, mouseIn = false, lastMouseT = -1e9, cursorA = 0;

    /* camera */
    var eyeX = 0, eyeY = 0, eyeZ = 0;
    var yaw = 0, pitch = 0;
    var rgt = new Float32Array(3), upv = new Float32Array(3), bck = new Float32Array(3);
    var rayDX = 0, rayDY = 0, rayDZ = -1;   /* pointer ray (world) */
    var urx = 0, ury = 0, urz = -1;          /* scratch unproject ray */

    var mProj = new Float32Array(16), mView = new Float32Array(16), mVP = new Float32Array(16);

    /* a11y text-column dim band (CSS px, fed to the shaders) */
    var dimL = 0, dimR = -1;

    /* GL objects (rebuilt wholesale on context restore) */
    var progBack = null, progBell = null, progRib = null, progSil = null;
    var progShell = null, progGlow = null, progAura = null;
    var progMote = null, progStar = null;
    var quadBuf = null, bellVBuf = null, bellIBuf = null;
    var ribVBuf = null, ribIBuf = null, silBuf = null, shellBuf = null;
    var moteBuf = null;
    var bellIdxCount = 0, ribIdxCount = 0;

    /* ---------------------------------------------------- the Medusa (hero) */
    var heroX = 0, heroY = 0, heroVX = 0, heroVY = 0;   /* anchor spring */
    var heroPX = 0, heroPY = 0;                         /* drawn position (with bob) */
    var heroScale = 2.2, heroFloor = 0, heroInited = false;
    var anchorPxX = 0, anchorPxY = 0;
    var pulsePhase = 0;              /* traveling contraction wave phase */
    var leanX = 0, leanZ = 0;        /* bell lean (radians) */
    var flare = 0;                   /* pulse brightness flare */
    var kink = 0;                    /* post-poke tentacle zigzag energy */
    var gonadPulse = 0;              /* contraction wave sampled at the gonad ring */
    var heroBright = 0.6, shaftGlow = 0.5, iridPh = 0, wobPh = 0, flutPh = 0;
    var rayPh = 0, starTw = 0, starInt = 0;   /* Ascension: bounded phases */
    var ripX = 0, ripY = 0, ripZ = 0, ripR = 0, ripA = 0;  /* mote click ripple */
    var heroU = 0.5, heroV = 0.5, heroR = 0.3, heroDk = 0;  /* backdrop contrast pocket */
    var m9 = new Float32Array(9);    /* bell model rot*scale, column-major */
    var m9i = new Float32Array(9);   /* its inverse (world dir -> bell local) */
    var mgX = 0, mgY = 0, mgZ = 0;   /* margin-point scratch */

    /* verlet chains: preallocated, zero per-frame allocation */
    var ndPos = new Float32Array(TOTAL_NODES * 3);
    var ndPrev = new Float32Array(TOTAL_NODES * 3);
    var chOff = new Int32Array(CHAINS), chLen = new Int32Array(CHAINS);
    var chPhi = new Float32Array(CHAINS), chSeg = new Float32Array(CHAINS);
    var chKind = new Uint8Array(CHAINS), chSeed = new Float32Array(CHAINS);
    (function () {
      var off = 0, c;
      for (c = 0; c < CHAINS; c++) {
        chOff[c] = off;
        if (c < CH_TENT) {
          chLen[c] = TENT_NODES; chKind[c] = 0;
          chPhi[c] = (c / CH_TENT) * TWO_PI + 0.13 + 0.06 * Math.sin(c * 7.3);
          off += TENT_NODES;
        } else {
          chLen[c] = ARM_NODES; chKind[c] = 1;
          chPhi[c] = 0.7853982 + (c - CH_TENT) * 1.5707963;
          off += ARM_NODES;
        }
        chSeed[c] = c * 2.399;
      }
    })();

    /* ribbon vertex stream: 2 verts per node, 7 floats per vert (PREALLOCATED;
       the only per-frame GPU upload besides the tiny shell pack) */
    var ribF = new Float32Array(TOTAL_NODES * 2 * 7);

    /* distant silhouettes: static instance data (x,y,z,scale | rate,seed,alpha,0) */
    var silF = new Float32Array(N_SIL * 8);
    (function () {
      var d = [
        [-13.0, 2.5, -33, 1.10, 0.90, 1.3, 0.42],
        [-5.0, -3.0, -36, 0.85, 1.05, 4.1, 0.34],
        [3.0, 4.2, -31, 0.70, 1.15, 2.2, 0.38],
        [9.0, -1.5, -37, 1.25, 0.80, 5.0, 0.30],
        [16.0, 3.0, -34, 0.95, 1.00, 0.6, 0.36],
        [-20.0, -0.5, -38, 1.40, 0.75, 3.3, 0.28]
      ];
      var i, o;
      for (i = 0; i < N_SIL; i++) {
        o = i * 8;
        silF[o] = d[i][0]; silF[o + 1] = d[i][1]; silF[o + 2] = d[i][2];
        silF[o + 3] = d[i][3];
        silF[o + 4] = d[i][4]; silF[o + 5] = d[i][5]; silF[o + 6] = d[i][6];
        silF[o + 7] = 0;
      }
    })();

    /* ascending star-motes: static instance data, preallocated once
       (ox, oy0, oz, size | rise rate, seed, twinkle rate, alpha) —
       radius biased inward so the dust is dense near her, sparse far.
       The motes' u_time is bounded: uploaded as timeS % MOTE_T, so every
       rate is quantized at build time to whole cycles per MOTE_T (rise to
       k*span/T, twinkle to k*2pi/T; granularity ~0.01 — imperceptible) and
       the wrap at T is seamless. Keeps fp32 phase math precise forever. */
    var MOTE_T = 600;                /* shared mote period, seconds */
    var N_MOTE = 90;                 /* buffer holds all; mobile draws fewer */
    var moteCount = N_MOTE;
    var moteF = new Float32Array(N_MOTE * 8);
    (function () {
      function fr(x) { var s = Math.sin(x) * 43758.5453; return s - Math.floor(s); }
      var i, o;
      for (i = 0; i < N_MOTE; i++) {
        o = i * 8;
        var rr = Math.pow(fr(i * 12.9898 + 1.3), 1.7) * 3.4 + 0.3;
        var aa = fr(i * 78.233 + 2.1) * TWO_PI;
        moteF[o] = Math.cos(aa) * rr;
        moteF[o + 1] = fr(i * 3.7 + 0.7) * 6.5;
        moteF[o + 2] = (fr(i * 9.1 + 4.2) - 0.5) * 2.4;
        moteF[o + 3] = 0.05 + 0.075 * fr(i * 5.3 + 3.3);
        moteF[o + 4] = Math.round((0.28 + 0.5 * fr(i * 7.7 + 0.2))
          * MOTE_T / 6.5) * 6.5 / MOTE_T;
        moteF[o + 5] = fr(i * 11.3 + 5.9) * TWO_PI;
        moteF[o + 6] = Math.round((1.4 + 2.6 * fr(i * 17.9 + 2.7))
          * MOTE_T / TWO_PI) * TWO_PI / MOTE_T;
        moteF[o + 7] = (0.30 + 0.45 * fr(i * 23.1 + 8.8)) * Math.max(0.25, 1.2 - 0.22 * rr);
      }
    })();

    /* shockwave shells (pulse), max 6 concurrent, slots reused */
    var shells = new Array(MAX_SHELL);
    var ji;
    for (ji = 0; ji < MAX_SHELL; ji++) {
      shells[ji] = { active: false, x: 0, y: 0, z: 0, r: 0, maxR: 0, speed: 0, delay: 0, strength: 0, alpha: 0 };
    }
    var shellF = new Float32Array(MAX_SHELL * 8);
    var shellCount = 0;
    var flash = { active: false, x: 0, y: 0, z: 0, t: 0, dur: 0.5 };

    var api = { mount: mount, pulse: pulse, setActivity: setActivity, detach: detach, onFatal: null };

    /* -------------------------------------------------------------- shaders */
    var BACK_VS = [
      'attribute vec2 a_p;',
      'varying vec2 v_uv;',
      'void main() {',
      '  gl_Position = vec4(a_p, 0.0, 1.0);',
      '  v_uv = a_p * 0.5 + 0.5;',
      '}'
    ].join('\n');

    var BACK_FS = [
      'precision mediump float;',
      'varying vec2 v_uv;',
      /* Shaft phases are precomputed sin() values uploaded per frame: an
         unbounded u_time in a mediump (possibly fp16) fragment shader
         quantizes within hours and overflows fp16 max (65504) -> NaN. */
      'uniform vec4 u_shaft;',
      'uniform float u_activity;',
      'uniform float u_aspect;',
      /* nature-documentary staging: a soft dark pocket of water directly
         behind the hero so the luminous creature pops off the frame */
      'uniform vec4 u_hero;',   /* uv.x, uv.y, radius (v units), strength */
      /* Ascension: every shaft is aimed at the Medusa — they spread apart at
         the surface and converge on her anchor (u_hero.xy), an annunciation
         column; the light dies just below her so she stands at its foot */
      'float shaft(vec2 uv, float off, float wBase, float sv) {',
      '  float hx = u_hero.x * u_aspect;',
      '  float dy = clamp((uv.y - u_hero.y) / max(1.08 - u_hero.y, 0.30), 0.0, 1.5);',
      '  float cx = hx + off * u_aspect * dy;',
      '  float w = wBase * (0.5 + dy * 2.0);',
      '  float d = abs(uv.x * u_aspect - cx) / max(w, 0.015);',
      '  float fall = max(0.0, 1.0 - d);',
      '  fall *= fall;',
      '  float fade = smoothstep(u_hero.y - 0.34, u_hero.y + 0.22, uv.y);',
      '  return fall * fade * (0.72 + 0.28 * sv);',
      '}',
      'void main() {',
      '  vec3 bot = vec3(0.016, 0.039, 0.071);',   /* #040A12 */
      '  vec3 mid = vec3(0.031, 0.094, 0.169);',   /* #08182B */
      '  vec3 top = vec3(0.043, 0.125, 0.212);',   /* #0B2036 */
      '  vec3 col = mix(bot, mid, smoothstep(0.0, 0.62, v_uv.y));',
      '  col = mix(col, top, smoothstep(0.62, 1.0, v_uv.y));',
      /* two inner shafts land gold on her crown; two outer stay cool and
         faint — the warmth belongs to the light that FOUND her */
      '  float sg = 0.0;',
      '  sg += shaft(v_uv, 0.02, 0.055, u_shaft.x) * 0.215;',
      '  sg += shaft(v_uv, -0.13, 0.085, u_shaft.y) * 0.135;',
      '  float sc = 0.0;',
      '  sc += shaft(v_uv, 0.17, 0.120, u_shaft.z) * 0.085;',
      '  sc += shaft(v_uv, -0.31, 0.150, u_shaft.w) * 0.060;',
      /* the heavens surge while the scan runs */
      '  float surge = 1.0 + 0.55 * u_activity;',
      /* white-gold grade lives in the LIGHT only (never the flesh) */
      '  col += vec3(0.95, 0.87, 0.68) * (sg * surge);',
      '  col += vec3(0.55, 0.72, 0.90) * (sc * surge);',
      '  col += vec3(0.5, 0.85, 1.0) * (u_activity * 0.05);',
      /* local darkening behind the hero (slightly elongated down the drape) */
      '  vec2 hd = vec2((v_uv.x - u_hero.x) * u_aspect, (v_uv.y - u_hero.y) * 0.8);',
      '  float hk = exp(-dot(hd, hd) / max(u_hero.z * u_hero.z, 1e-4));',
      '  col *= 1.0 - u_hero.w * hk;',
      /* radial falloff so the corners breathe dark, like the 2D backdrop */
      '  vec2 cc = vec2((v_uv.x - 0.5) * u_aspect, v_uv.y - 0.45);',
      '  col *= 1.0 - 0.35 * smoothstep(0.35, 1.15, length(cc));',
      /* fine photographic grain: kills the flat-gradient CG look of the water */
      '  float grain = fract(sin(dot(gl_FragCoord.xy, vec2(12.9898, 78.233))) * 43758.5453);',
      '  col += (grain - 0.5) * 0.010;',
      '  gl_FragColor = vec4(col, 1.0);',
      '}'
    ].join('\n');

    var DIM_GLSL = [
      'float dimBand(float px) {',
      '  if (u_dim.y < u_dim.x) { return 1.0; }',
      '  float t = 0.0;',
      '  if (px < u_dim.x) { t = (u_dim.x - px) / 120.0; }',
      '  else if (px > u_dim.y) { t = (px - u_dim.y) / 120.0; }',
      '  t = clamp(t, 0.0, 1.0);',
      '  t = t * t * (3.0 - 2.0 * t);',
      '  return 0.35 + 0.65 * t;',
      '}'
    ].join('\n');

    /* The Medusa's bell: generated in the vertex shader from (t, phi) so the
       traveling contraction wave produces CORRECT normals (finite differences
       of the deformed surface), not a squashed static mesh. The same margin
       math is mirrored in JS (bellMarginLocal) to anchor the tentacle roots. */
    var BELL_FN = [
      'vec3 bellPt(float t, float phi) {',
      '  float wx = u_pw - t * 2.2;',            /* wave travels apex -> margin */
      '  float ws = sin(wx);',
      '  float ctr = max(ws, 0.0);',
      '  ctr = ctr * ctr * (0.55 + 0.45 * ctr);',/* fast power stroke, slow recovery */
      '  float th = t * 2.05;',                  /* past the equator: margin tucks under */
      '  float r = sin(th);',
      '  float y = cos(th) * 0.60;',             /* moon-jelly aspect: wide, shallow */
      '  float sc = smoothstep(0.5, 1.0, t);',
      '  float lobe = cos(8.0 * phi);',
      '  r += 0.062 * sc * sc * lobe * r;',      /* 8-lobe scalloped margin */
      '  y -= 0.075 * sc * sc * (0.5 + 0.5 * lobe);', /* lappet tips droop, notches ride high */
      /* lappet flutter: high-frequency, low-amplitude wave riding the rim */
      '  float fl = sc * sc * sc;',
      '  y += 0.015 * fl * sin(24.0 * phi + u_fl + 2.0 * lobe);',
      '  r += 0.006 * fl * sin(24.0 * phi - u_fl + 1.1);',
      '  r *= 1.0 - 0.22 * ctr * smoothstep(0.1, 0.9, t);',
      '  y += 0.06 * ctr * (1.0 - smoothstep(0.0, 0.7, t));',
      '  y -= 0.13 * ctr * sc;',
      '  return vec3(r * cos(phi), y, r * sin(phi));',
      '}'
    ].join('\n');

    var BELL_VS = [
      'attribute vec2 a_tp;',
      'uniform mat4 u_vp;',
      'uniform vec3 u_pos;',
      'uniform mat3 u_m;',
      'uniform float u_pw;',
      'uniform vec2 u_dim;',
      'uniform float u_vpw;',
      'uniform float u_floor;',
      'uniform float u_fl;',
      'varying vec2 v_tp;',
      'varying vec3 v_n;',
      'varying vec3 v_wp;',
      'varying vec3 v_lp;',
      'varying float v_dim;',
      DIM_GLSL,
      BELL_FN,
      'void main() {',
      '  float t = a_tp.x;',
      '  float phi = a_tp.y;',
      '  vec3 p0 = bellPt(t, phi);',
      '  float tn = max(t, 0.02);',              /* apex: r=0 kills the phi tangent */
      '  vec3 q0 = bellPt(tn, phi);',
      '  vec3 qT = bellPt(tn + 0.012, phi);',
      '  vec3 qP = bellPt(tn, phi + 0.02);',
      '  vec3 nl = cross(qP - q0, qT - q0);',
      '  vec3 world = u_pos + u_m * p0;',
      '  gl_Position = u_vp * vec4(world, 1.0);',
      '  v_n = normalize(u_m * nl);',
      '  v_wp = world;',
      '  v_lp = p0;',
      '  v_tp = a_tp;',
      '  vec4 cc = u_vp * vec4(u_pos, 1.0);',
      '  float px = (cc.x / max(cc.w, 0.0001) * 0.5 + 0.5) * u_vpw;',
      '  v_dim = max(dimBand(px), u_floor);',
      '}'
    ].join('\n');

    var BELL_FS = [
      'precision mediump float;',
      'varying vec2 v_tp;',
      'varying vec3 v_n;',
      'varying vec3 v_wp;',
      'varying vec3 v_lp;',
      'varying float v_dim;',
      'uniform vec3 u_cam;',
      'uniform float u_glow;',
      'uniform float u_sg;',    /* light-shaft brightness 0..1 (keys the SSS) */
      'uniform float u_ir;',    /* bounded iridescence shimmer phase */
      'uniform float u_gp;',    /* contraction wave at the gonad ring (CPU) */
      'uniform float u_pass;',  /* 0 = far (back) wall, 1 = near (front) wall */
      'uniform mat3 u_mi;',     /* inverse bell model: world dir -> bell local */
      /* value noise for the mesoglea: continuous in bell-local space (no seam) */
      'float vhash(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }',
      'float vnoise(vec2 p) {',
      '  vec2 i = floor(p); vec2 f = fract(p);',
      '  f = f * f * (3.0 - 2.0 * f);',
      '  return mix(mix(vhash(i), vhash(i + vec2(1.0, 0.0)), f.x),',
      '             mix(vhash(i + vec2(0.0, 1.0)), vhash(i + vec2(1.0, 1.0)), f.x), f.y);',
      '}',
      /* soft-knee tonemap (Reinhard, white point 2.4): bright cores stay
         luminous and colored instead of clipping to white soup */
      'vec3 tonemap(vec3 c) { return c * (vec3(1.0) + c * 0.1736) / (vec3(1.0) + c); }',
      'void main() {',
      '  float t = v_tp.x;',
      '  float phi = v_tp.y;',
      '  vec3 n = normalize(v_n);',
      '  vec3 v = normalize(v_wp - u_cam);',
      '  float nds = dot(n, v);',
      /* two-pass translucency: each pass keeps only its own wall */
      '  if (u_pass > 0.5) { if (nds >= 0.0) { gl_FragColor = vec4(0.0); return; } }',
      '  else if (nds < 0.0) { gl_FragColor = vec4(0.0); return; }',
      '  vec3 ns = nds < 0.0 ? n : -n;',        /* shading normal, toward the eye */
      '  float ndv = abs(nds);',
      /* ---- mesoglea: procedural density + micro-wrinkles near the margin */
      '  float dens = vnoise(v_lp.xz * 5.0) + 0.5 * vnoise(v_lp.zx * 11.0 + 3.7);',
      '  dens = 0.66 + 0.46 * dens;',           /* ~0.66..1.35 tissue thickness */
      '  float mgz = smoothstep(0.60, 0.94, t);',
      '  float wrk = sin(phi * 64.0 + 3.0 * sin(t * 26.0));',
      '  float ndw = clamp(ndv + wrk * 0.09 * mgz, 0.0, 1.0);',
      '  vec3 L = normalize(vec3(0.30, 1.0, 0.16));',  /* the shafts, from above */
      /* ---- wrap-light SSS keyed to the shafts: light bleeds around the limb */
      '  float wr = clamp((dot(ns, L) + 0.85) / 1.85, 0.0, 1.0);',
      '  wr *= wr;',
      '  float sss = wr * (0.40 + 0.60 * u_sg);',
      /* forward transmission: shaft light through the body toward the eye */
      '  float trn = pow(clamp(dot(v, -L), 0.0, 1.0), 1.5);',
      '  vec3 body = vec3(0.52, 0.40, 0.72);',  /* medusa violet */
      '  vec3 deep = vec3(0.13, 0.09, 0.27);',
      '  vec3 col = mix(deep, body, sss) * (0.10 + 0.30 * sss) * dens;',
      '  col += body * trn * (0.16 + 0.20 * u_sg) * dens;',
      /* directional daylight: the dome top visibly brighter than the skirt */
      '  float dl = max(dot(ns, L), 0.0);',
      '  col += vec3(0.50, 0.58, 0.82) * dl * dl * (0.10 + 0.18 * u_sg) * dens;',
      /* multiple-scattering translucency: light pours in at the apex and
         diffuses down the jelly — the top of a real bell GLOWS, whatever the
         viewing angle (single-scatter trn is ~0 at 90 degrees to the shafts) */
      '  float litv = clamp(0.5 + 0.83 * v_lp.y, 0.0, 1.0);',
      '  col += vec3(0.58, 0.55, 0.80) * litv * litv * (0.14 + 0.20 * u_sg) * dens;',
      /* micro-wrinkles also catch the light directly */
      '  col *= 1.0 + 0.055 * wrk * mgz;',
      /* skirt micro-ring striations: a band of fine concentric rings just
         below the margin, noise-broken so they read as tissue, not print */
      '  float srng = sin(t * 140.0 + (vnoise(vec2(phi * 2.6, t * 7.0)) - 0.5) * 6.0);',
      '  srng = srng * srng * smoothstep(0.55, 0.84, t) * (1.0 - smoothstep(0.94, 1.0, t));',
      '  col += vec3(0.34, 0.30, 0.52) * srng * 0.10;',
      /* interior volume glow: light scattered inside the jelly, seen on the
         far wall through the front — the bell must not be a dark void */
      '  col += body * (1.0 - u_pass) * (0.10 + 0.14 * u_sg) * (0.6 + 0.4 * sss) * dens;',
      /* ---- Blinn-Phong wet-glass sheen: tight gleam + broad satin lobe */
      '  vec3 hv = normalize(L - v);',
      '  float ndh = max(dot(ns, hv), 0.0);',
      '  float spec = pow(ndh, 200.0) * 3.2 + pow(ndh, 24.0) * 0.30;',
      /* faint second catchlight: broken water surface, off-axis */
      '  vec3 h2 = normalize(normalize(vec3(-0.45, 0.75, 0.35)) - v);',
      '  spec += pow(max(dot(ns, h2), 0.0), 60.0) * 0.35;',
      '  spec *= (0.40 + 0.60 * u_sg) * (0.35 + 0.65 * u_pass);',
      '  spec *= 1.0 - 0.35 * mgz * (0.5 + 0.5 * wrk);',  /* wrinkles break the gleam */
      '  col += vec3(0.80, 0.90, 1.0) * spec;',
      /* ---- 3-sample spectral rim: blue refracts widest, red hugs the edge */
      '  float g1 = 1.0 - ndw;',
      '  float rimR = pow(g1, 3.6);',
      '  float rimG = pow(g1, 2.7);',
      '  float rimB = pow(g1, 2.15);',
      '  col += vec3(0.40 * rimR + 0.06 * rimB, 0.55 * rimG, 0.85 * rimB) * 0.62;',
      '  col += vec3(0.55, 0.42, 0.80) * pow(g1, 1.4) * 0.15;',
      /* grazing iridescence, whisper-level */
      '  float grz = pow(g1, 3.2);',
      '  col += vec3(0.5 + 0.5 * sin(12.0 * ndv + u_ir),',
      '              0.5 + 0.5 * sin(12.0 * ndv + u_ir + 2.09),',
      '              0.5 + 0.5 * sin(12.0 * ndv + u_ir + 4.19)) * grz * 0.05;',
      /* ------------- interior anatomy with true parallax against the shell */
      '  vec3 vl = normalize(u_mi * v);',       /* view dir in bell-local space */
      '  float pj = clamp((v_lp.y - 0.08) / max(-vl.y, 0.22), 0.0, 1.3);',
      '  pj *= u_pass;',                         /* far wall: organs sit on it */
      '  vec2 q = v_lp.xz + vl.xz * pj;',
      '  float rr = length(q);',
      '  float face = smoothstep(0.18, 0.55, ndv);',
      /* four-lobed gonad clover (horseshoes opening toward the center) */
      '  float gon = 0.0;',
      '  for (int k = 0; k < 4; k++) {',
      '    float ga = 0.7854 + float(k) * 1.5708;',
      '    vec2 ck = vec2(cos(ga), sin(ga)) * 0.36;',
      '    vec2 gp = q - ck;',
      '    float gd = length(gp);',
      '    float ring = exp(-pow((gd - 0.135) / 0.045, 2.0));',
      '    float fill = smoothstep(0.150, 0.100, gd);',
      '    float open = 1.0 - smoothstep(0.15, 0.75, dot(gp / max(gd, 0.001), -ck / 0.36));',
      '    gon += (ring * 0.85 + fill * 0.45) * (0.25 + 0.75 * open);',
      '  }',
      '  gon *= 0.9 + 0.35 * vnoise(q * 14.0);', /* granular gonad tissue */
      '  float organW = mix(0.30, 1.0, u_pass);',/* faint via the far wall */
      '  col += vec3(0.92, 0.58, 0.66) * gon * face * organW * (0.85 + 0.60 * u_gp);',
      /* 16 fine radial canals, fading toward the margin (on the shell wall) */
      '  float can = pow(abs(cos(phi * 8.0)), 48.0);',
      '  can *= smoothstep(0.10, 0.24, t) * (1.0 - 0.55 * t);',
      '  col += vec3(0.62, 0.72, 0.92) * can * face * 0.28;',
      /* gastric nucleus + four-pouch cross, floating on the organ plane */
      '  float nuc = exp(-rr * rr / 0.010);',
      '  float phc = atan(q.y, q.x);',
      '  float cr4 = pow(abs(cos(phc * 2.0 + 0.7854)), 6.0) * exp(-pow((rr - 0.10) / 0.07, 2.0));',
      '  col += vec3(0.90, 0.82, 0.95) * (nuc * 0.40 + cr4 * 0.28) * face * organW;',
      /* coronal muscle rings near the margin */
      '  float mus = pow(abs(sin(t * 44.0)), 6.0) * smoothstep(0.55, 0.9, t);',
      '  col += vec3(0.45, 0.40, 0.70) * mus * 0.06;',
      /* marginal nerve ring + rhopalia specks in the lappet notches */
      '  float mring = exp(-pow((t - 0.965) / 0.02, 2.0));',
      '  col += vec3(0.40, 0.62, 0.82) * mring * 0.14;',
      '  float rho = pow(0.5 - 0.5 * cos(8.0 * phi), 24.0) * mring;',
      '  col += vec3(0.80, 0.90, 1.0) * rho * 0.25;',
      /* translucent body: alpha dims what is behind (premultiplied over) */
      '  float al = (0.12 + 0.18 * ndw + 0.15 * gon * face * u_pass) * dens;',
      '  al *= mix(0.38, 1.0, u_pass);',        /* the far wall barely occludes */
      '  col *= mix(0.72, 1.0, u_pass);',
      /* far-wall margin dissolve: the back wall melts to nothing toward its
         rim so no creased polygon edge ever reads through the near glass */
      '  float bkFade = 1.0 - (1.0 - u_pass) * smoothstep(0.68, 0.96, t);',
      '  col *= bkFade;',
      '  al *= bkFade;',
      '  col *= u_glow * v_dim;',
      '  col = tonemap(col);',
      '  al = clamp(al, 0.0, 0.62) * v_dim;',
      '  gl_FragColor = vec4(col, al);',
      '}'
    ].join('\n');

    /* verlet tentacle / oral-arm ribbons: positions computed on the CPU,
       streamed once per frame from the preallocated ribF */
    var RIB_VS = [
      'attribute vec3 a_pos;',
      'attribute vec4 a_aux;',   /* u along, side -1..1, brightness, kind */
      'uniform mat4 u_vp;',
      'uniform vec2 u_dim;',
      'uniform float u_vpw;',
      'uniform float u_floor;',
      'varying vec4 v_aux;',
      'varying float v_dim;',
      DIM_GLSL,
      'void main() {',
      '  gl_Position = u_vp * vec4(a_pos, 1.0);',
      '  v_aux = a_aux;',
      '  float px = (gl_Position.x / max(gl_Position.w, 0.0001) * 0.5 + 0.5) * u_vpw;',
      '  v_dim = max(dimBand(px), u_floor);',
      '}'
    ].join('\n');

    var RIB_FS = [
      'precision mediump float;',
      'varying vec4 v_aux;',
      'varying float v_dim;',
      'uniform float u_wob;',
      'void main() {',
      '  float u = v_aux.x;',
      '  float vv = v_aux.y;',
      '  float br = v_aux.z * v_dim;',
      '  vec3 col;',
      '  float a;',
      '  if (v_aux.w < 0.5) {',
      /* fine marginal tentacle: soft round core + wet specular filament;
         nematocyst beads swell/shimmer down the strand (per-chain phase) */
      '    float prof = 1.0 - vv * vv;',
      '    float bead = 0.82 + 0.30 * sin(u * 46.0 - u_wob * 2.0 + v_aux.w * 500.0);',
      '    a = prof * prof * (1.0 - 0.78 * u) * br * 0.42 * bead;',
      '    a += pow(prof, 8.0) * (1.0 - 0.6 * u) * br * 0.30;',
      '    a *= 0.45 + 0.55 * smoothstep(0.0, 0.18, u);',  /* roots melt into the rim */
      /* violet root -> faint cyan tip: depth cue without breaking rim discipline */
      '    col = mix(vec3(0.60, 0.48, 0.84), vec3(0.22, 0.56, 0.80), smoothstep(0.35, 1.0, u) * 0.65);',
      '    col = mix(col, vec3(0.82, 0.88, 1.0), pow(prof, 8.0) * 0.5);',
      /* brighter wet core line: the strand's inner thread stays waterlit */
      '    col += vec3(0.26, 0.30, 0.42) * pow(prof, 6.0) * (1.0 - 0.55 * u) * 0.7;',
      '  } else {',
      /* frilled oral arm: soft curtain, ruffle brightens in traveling folds;
         third lace octave + lit hem line give the frill legible structure */
      '    float armPh = (v_aux.w - 1.0) * 300.0;',
      '    float edge = abs(vv);',
      '    float fold = 0.5 + 0.5 * sin(u * 21.0 + u_wob + armPh);',
      '    float fold2 = 0.5 + 0.5 * sin(u * 9.0 - u_wob + armPh * 1.7 + 2.1);',
      '    float fold3 = 0.5 + 0.5 * sin(u * 33.0 + u_wob + armPh * 2.3);',
      /* integer u_wob multiplier: wobPh wraps at 2pi, so non-integer rates
         would pop the lace phase at every wrap (de-sync comes from armPh) */
      '    float lace = 0.5 + 0.5 * sin(u * 47.0 - u_wob * 2.0 + armPh * 3.1);',
      '    float body2 = (1.0 - edge * edge);',
      '    float hem = smoothstep(1.0, 0.55, edge + 0.26 * fold + 0.10 * fold3 + 0.05 * lace);',
      '    float hemGlow = smoothstep(0.45, 0.9, edge) * hem;',
      '    a = body2 * hem * (0.22 + 0.50 * fold * fold2 + 0.20 * fold2 + 0.14 * fold3 + 0.14 * lace * fold)',
      '      * (1.0 - 0.42 * u) * (1.0 - smoothstep(0.66, 0.98, u)) * br * 0.62;',
      /* edge masks: the curtain must never end on a razor-straight quad edge —
         roots melt into the manubrium, tips dissolve to water, sides feather
         beyond the body2 profile (kills the hard silhouettes seen through
         the bell), and the result is clamped so it can never darken */
      '    a *= smoothstep(0.0, 0.16, u);',
      '    a *= smoothstep(1.0, 0.72, edge);',
      '    a = max(a, 0.0);',
      '    col = mix(vec3(0.62, 0.50, 0.82), vec3(0.78, 0.64, 0.88), fold);',
      /* the ruffled hem catches the key light */
      '    col += vec3(0.52, 0.44, 0.68) * hemGlow * lace * 0.55;',
      '  }',
      '  gl_FragColor = vec4(col * a, 1.0);',
      '}'
    ].join('\n');

    /* distant silhouettes: the whole population is a static instance buffer;
       drift + breathing are functions of u_time (zero CPU cost per frame) */
    var SIL_VS = [
      'attribute vec2 a_tp;',
      'attribute vec4 a_i0;',   /* x, y, z, scale */
      'attribute vec4 a_i1;',   /* rate, seed, alpha, - */
      'uniform mat4 u_vp;',
      'uniform vec3 u_cam;',
      'uniform float u_time;',
      'uniform vec2 u_dim;',
      'uniform float u_vpw;',
      'uniform float u_fogD;',
      'varying float v_a;',
      DIM_GLSL,
      'void main() {',
      '  float t = a_tp.x;',
      '  float phi = a_tp.y;',
      '  float ph = u_time * a_i1.x + a_i1.y;',
      '  float s = sin(ph);',
      '  float ctr = max(s, 0.0); ctr *= ctr;',
      '  float th = t * 2.05;',
      '  float r = sin(th) * (1.0 - 0.20 * ctr);',
      '  float y = cos(th) * 0.60 * (1.0 + 0.12 * ctr);',
      '  vec3 p = vec3(r * cos(phi), y, r * sin(phi)) * a_i0.w;',
      '  vec3 world = a_i0.xyz + p + vec3(sin(u_time * 0.05 + a_i1.y) * 1.8,',
      '    sin(u_time * 0.04 + a_i1.y * 1.7) * 2.4, 0.0);',
      '  gl_Position = u_vp * vec4(world, 1.0);',
      '  vec4 cc = u_vp * vec4(a_i0.xyz, 1.0);',
      '  float px = (cc.x / max(cc.w, 0.0001) * 0.5 + 0.5) * u_vpw;',
      '  float dist = length(a_i0.xyz - u_cam);',
      '  v_a = a_i1.z * exp(-dist * u_fogD) * dimBand(px);',
      '}'
    ].join('\n');

    var SIL_FS = [
      'precision mediump float;',
      'varying float v_a;',
      'void main() {',
      '  gl_FragColor = vec4(vec3(0.40, 0.35, 0.60) * v_a, 1.0);',
      '}'
    ].join('\n');

    var SHELL_VS = [
      'attribute vec2 a_q;',
      'attribute vec4 a_i0;',   /* x, y, z, radius */
      'attribute vec4 a_i1;',   /* alpha, -, -, - */
      'uniform mat4 u_vp;',
      'uniform vec3 u_right;',
      'uniform vec3 u_up;',
      'varying vec2 v_q;',
      'varying float v_a;',
      'void main() {',
      '  vec3 world = a_i0.xyz + (u_right * a_q.x + u_up * a_q.y) * a_i0.w;',
      '  gl_Position = u_vp * vec4(world, 1.0);',
      '  v_q = a_q;',
      '  v_a = a_i1.x;',
      '}'
    ].join('\n');

    var SHELL_FS = [
      'precision mediump float;',
      'varying vec2 v_q;',
      'varying float v_a;',
      /* u_dimD: text-column dim band in DEVICE pixels (x=left, y=right,
         z=feather). Shell quads can span the whole screen, so the band is
         evaluated per-fragment via gl_FragCoord — a vertex-interpolated
         factor would miss the band between the quad corners. Keeps peak
         shell glow from washing out bare rail labels (SURFACE/CONSOLE). */
      'uniform vec3 u_dimD;',
      'float dimBand(float px) {',
      '  if (u_dimD.y < u_dimD.x) { return 1.0; }',
      '  float t = 0.0;',
      '  if (px < u_dimD.x) { t = (u_dimD.x - px) / u_dimD.z; }',
      '  else if (px > u_dimD.y) { t = (px - u_dimD.y) / u_dimD.z; }',
      '  t = clamp(t, 0.0, 1.0);',
      '  t = t * t * (3.0 - 2.0 * t);',
      '  return 0.35 + 0.65 * t;',
      '}',
      'void main() {',
      '  float r = length(v_q);',
      '  float band = max(0.0, 1.0 - abs(r - 0.82) / 0.14);',
      '  band *= band;',
      '  float dim = dimBand(gl_FragCoord.x);',
      /* under the dim band, also pull the near-white core back toward the
         base cyan so the ring never approaches label-swallowing white */
      '  vec3 col = mix(vec3(0.0, 0.643, 0.863), vec3(0.918, 0.957, 1.0), band * 0.6 * dim);',
      '  gl_FragColor = vec4(col * band * v_a * dim, 1.0);',
      '}'
    ].join('\n');

    var GLOW_VS = [
      'attribute vec2 a_q;',
      'uniform mat4 u_vp;',
      'uniform vec3 u_pos;',
      'uniform vec3 u_right;',
      'uniform vec3 u_up;',
      'uniform float u_size;',
      'varying vec2 v_q;',
      'void main() {',
      '  vec3 world = u_pos + (u_right * a_q.x + u_up * a_q.y) * u_size;',
      '  gl_Position = u_vp * vec4(world, 1.0);',
      '  v_q = a_q;',
      '}'
    ].join('\n');

    var GLOW_FS = [
      'precision mediump float;',
      'varying vec2 v_q;',
      'uniform vec3 u_color;',
      'uniform float u_int;',
      'void main() {',
      '  float r = length(v_q);',
      '  float a = exp(-r * r * 4.0) * smoothstep(1.0, 0.65, r) * u_int;',
      '  gl_FragColor = vec4(u_color * a, 1.0);',
      '}'
    ].join('\n');

    /* layered hero aura + aureole + local background darkening in ONE
       billboard draw: rgb adds three nested glow lobes plus the Ascension
       glory (soft white-gold corona ring with slow animated rays) behind
       the bell, alpha (premultiplied-over blending) pulls the water down
       behind the bell for contrast staging */
    var AURA_FS = [
      'precision mediump float;',
      'varying vec2 v_q;',
      'uniform float u_int;',
      'uniform float u_pulse;',
      'uniform float u_floor;',
      'uniform float u_ray;',   /* bounded slow ray-rotation phase */
      'uniform vec3 u_dimD;',   /* dim band in DEVICE px (left, right, feather) */
      'float dimBand(float px) {',
      '  if (u_dimD.y < u_dimD.x) { return 1.0; }',
      '  float t = 0.0;',
      '  if (px < u_dimD.x) { t = (u_dimD.x - px) / u_dimD.z; }',
      '  else if (px > u_dimD.y) { t = (px - u_dimD.y) / u_dimD.z; }',
      '  t = clamp(t, 0.0, 1.0);',
      '  t = t * t * (3.0 - 2.0 * t);',
      '  return 0.35 + 0.65 * t;',
      '}',
      'void main() {',
      '  float r = length(v_q);',
      '  float dim = max(dimBand(gl_FragCoord.x), u_floor);',
      /* local background darkening, faded to zero before the quad boundary
         (max(|x|,|y|) tracks the square edge) so the alpha cutoff never
         paints a vertical seam in the water beside her */
      '  float dk = exp(-r * r * 1.2) * 0.42'
        + ' * (1.0 - smoothstep(0.70, 0.98, max(abs(v_q.x), abs(v_q.y))));',
      '  float br = u_int * (0.85 + 0.35 * u_pulse);',
      '  vec3 col = vec3(0.42, 0.30, 0.62) * exp(-r * r * 22.0) * 0.26',
      '           + vec3(0.24, 0.40, 0.62) * exp(-r * r * 7.0) * 0.11',
      '           + vec3(0.10, 0.28, 0.33) * exp(-r * r * 2.6) * 0.05;',
      /* the aureole: a diffuse glory behind the bell — soft radiant ring,
         faint slow counter-rotating rays, white-gold, never a hard halo */
      '  float ang = atan(v_q.y, v_q.x + 1e-5);',  /* atan(0,0) is undefined */
      '  float ring = exp(-pow((r - 0.42) / 0.21, 2.0));',
      '  float rays = 0.42 + 0.34 * sin(ang * 10.0 + u_ray * 2.0)',
      '             + 0.24 * sin(ang * 5.0 - u_ray * 3.0 + 1.7);',
      '  float glory = ring * rays + exp(-pow((r - 0.26) / 0.20, 2.0)) * 0.40;',
      '  col += vec3(0.97, 0.88, 0.68) * glory * 0.32;',
      '  gl_FragColor = vec4(col * br * dim, dk);',
      '}'
    ].join('\n');

    /* ascending star-motes: celestial dust RISING around the Medusa (the
       inverse of marine snow), dense near her, sparse far away. The whole
       population is one static instance buffer; rise/drift/twinkle are
       bounded functions of u_time. Unlike the silhouettes, u_time here is
       BOUNDED (timeS % MOTE_T) — all rates are whole cycles per MOTE_T. */
    var MOTE_VS = [
      'attribute vec2 a_q;',
      'attribute vec4 a_i0;',   /* ox, oy0 (rise offset), oz, size */
      'attribute vec4 a_i1;',   /* rise rate, seed, twinkle rate, alpha */
      'uniform mat4 u_vp;',
      'uniform vec3 u_right;',
      'uniform vec3 u_up;',
      'uniform vec3 u_hpos;',
      'uniform float u_time;',
      'uniform float u_hs;',
      'uniform float u_act;',
      /* click ripple: brightness travels through nearby motes (xyz origin,
         w expanding radius; u_ripA amplitude — all bounded, CPU-driven) */
      'uniform vec4 u_rip;',
      'uniform float u_ripA;',
      'uniform vec2 u_dim;',
      'uniform float u_vpw;',
      'uniform float u_floor;',
      'varying vec2 v_q;',
      'varying float v_a;',
      DIM_GLSL,
      'void main() {',
      '  float span = 6.5;',
      '  float yy = mod(a_i0.y + u_time * a_i1.x, span);',
      '  float lu = yy / span;',
      /* she attracts the light: motes drift toward her axis as they rise */
      '  float cvg = 1.0 - 0.30 * lu;',
      '  vec3 base = u_hpos + vec3(a_i0.x * u_hs * cvg, yy - span * 0.42, a_i0.z * u_hs * cvg);',
      /* sway rate = 2pi*48/MOTE_T (48 whole cycles per 600 s period) so
         the u_time wrap at MOTE_T is seamless */
      '  base.x += sin(u_time * 0.50265482 + a_i1.y) * 0.14 * u_hs;',
      '  float tw = 0.35 + 0.65 * (0.5 + 0.5 * sin(u_time * a_i1.z + a_i1.y * 5.0));',
      '  tw *= tw;',
      /* fade in low, fade out high: no popping at the wrap seam */
      '  float lf = smoothstep(0.0, 0.12, lu) * (1.0 - smoothstep(0.80, 1.0, lu));',
      '  float rd = distance(base, u_rip.xyz);',
      '  float rip = u_ripA * exp(-pow((rd - u_rip.w) * 0.7, 2.0));',
      '  vec3 world = base + (u_right * a_q.x + u_up * a_q.y) * (a_i0.w * u_hs);',
      '  gl_Position = u_vp * vec4(world, 1.0);',
      '  vec4 cc = u_vp * vec4(base, 1.0);',
      '  float px = (cc.x / max(cc.w, 0.0001) * 0.5 + 0.5) * u_vpw;',
      '  v_a = a_i1.w * tw * lf * (0.78 + 0.44 * u_act) * (1.0 + 2.4 * rip)',
      '      * max(dimBand(px), u_floor);',
      '  v_q = a_q;',
      '}'
    ].join('\n');

    var MOTE_FS = [
      'precision mediump float;',
      'varying vec2 v_q;',
      'varying float v_a;',
      'void main() {',
      '  float r = length(v_q);',
      '  float core = exp(-r * r * 9.0);',
      '  float cr = (exp(-abs(v_q.x) * 8.0) + exp(-abs(v_q.y) * 8.0)) * exp(-r * r * 2.2) * 0.30;',
      '  float a = (core + cr) * v_a;',
      '  gl_FragColor = vec4(vec3(0.95, 0.89, 0.75) * a, 1.0);',
      '}'
    ].join('\n');

    /* apex star-core: the bell's nucleus elevated to a star-like point with
       a subtle 4-point sparkle, drawn over the glass so it reads as a star */
    var STAR_FS = [
      'precision mediump float;',
      'varying vec2 v_q;',
      'uniform float u_int;',
      'uniform float u_tw;',    /* bounded twinkle phase */
      'uniform float u_floor;',
      'uniform vec3 u_dimD;',   /* dim band in DEVICE px (left, right, feather) */
      'float dimBand(float px) {',
      '  if (u_dimD.y < u_dimD.x) { return 1.0; }',
      '  float t = 0.0;',
      '  if (px < u_dimD.x) { t = (u_dimD.x - px) / u_dimD.z; }',
      '  else if (px > u_dimD.y) { t = (px - u_dimD.y) / u_dimD.z; }',
      '  t = clamp(t, 0.0, 1.0);',
      '  t = t * t * (3.0 - 2.0 * t);',
      '  return 0.35 + 0.65 * t;',
      '}',
      'void main() {',
      '  float r = length(v_q);',
      '  float core = exp(-r * r * 42.0) * 1.5;',
      '  float w1 = 0.85 + 0.15 * sin(u_tw);',
      '  float rays = exp(-abs(v_q.x) * 30.0) * exp(-abs(v_q.y) * 4.5)',
      '             + exp(-abs(v_q.y) * 30.0) * exp(-abs(v_q.x) * 4.5);',
      '  rays *= w1 * smoothstep(1.0, 0.2, r) * 0.55;',
      '  float halo = exp(-r * r * 6.0) * 0.22;',
      '  float dim = max(dimBand(gl_FragCoord.x), u_floor);',
      '  float a = (core + rays + halo) * u_int * dim;',
      '  vec3 gold = vec3(0.95, 0.89, 0.75);',
      '  gl_FragColor = vec4(mix(gold, vec3(1.0, 0.98, 0.92), core) * a, 1.0);',
      '}'
    ].join('\n');

    /* ----------------------------------------------------------- GL helpers */
    function compileShader(type, src) {
      var s = gl.createShader(type);
      gl.shaderSource(s, src);
      gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS) && !gl.isContextLost()) {
        gl.deleteShader(s);
        return null;
      }
      return s;
    }

    function makeProg(vsSrc, fsSrc, attrs, uniforms) {
      var vs = compileShader(gl.VERTEX_SHADER, vsSrc);
      if (!vs) { return null; }
      var fs = compileShader(gl.FRAGMENT_SHADER, fsSrc);
      if (!fs) { gl.deleteShader(vs); return null; }
      var p = gl.createProgram();
      var name;
      for (name in attrs) {
        if (Object.prototype.hasOwnProperty.call(attrs, name)) {
          gl.bindAttribLocation(p, attrs[name], name);
        }
      }
      gl.attachShader(p, vs);
      gl.attachShader(p, fs);
      gl.linkProgram(p);
      gl.deleteShader(vs);
      gl.deleteShader(fs);
      if (!gl.getProgramParameter(p, gl.LINK_STATUS) && !gl.isContextLost()) {
        gl.deleteProgram(p);
        return null;
      }
      var obj = { p: p };
      var i;
      for (i = 0; i < uniforms.length; i++) {
        obj[uniforms[i]] = gl.getUniformLocation(p, uniforms[i]);
      }
      return obj;
    }

    function setDiv(loc, d) {
      if (isGL2) { gl.vertexAttribDivisor(loc, d); }
      else { instExt.vertexAttribDivisorANGLE(loc, d); }
    }

    function drawElemInst(count, prim) {
      if (isGL2) { gl.drawElementsInstanced(gl.TRIANGLES, count, gl.UNSIGNED_SHORT, 0, prim); }
      else { instExt.drawElementsInstancedANGLE(gl.TRIANGLES, count, gl.UNSIGNED_SHORT, 0, prim); }
    }

    function drawArrInst(count, prim) {
      if (isGL2) { gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, count, prim); }
      else { instExt.drawArraysInstancedANGLE(gl.TRIANGLE_STRIP, 0, count, prim); }
    }

    /* --------------------------------------------------------------- meshes */
    /* the bell grid holds only (t, phi): the surface itself lives in the VS */
    function buildBellGrid() {
      var cols = BELL_SEG + 1;
      var rows = BELL_RINGS + 1;
      var v = new Float32Array(rows * cols * 2);
      var o = 0, r, s;
      for (r = 0; r < rows; r++) {
        var t = r / BELL_RINGS;
        for (s = 0; s < cols; s++) {
          v[o++] = t;
          v[o++] = (s / BELL_SEG) * TWO_PI;
        }
      }
      var idx = new Uint16Array(BELL_RINGS * BELL_SEG * 6);
      o = 0;
      for (r = 0; r < BELL_RINGS; r++) {
        for (s = 0; s < BELL_SEG; s++) {
          var a = r * cols + s, b = a + 1, c = a + cols, d = c + 1;
          idx[o++] = a; idx[o++] = c; idx[o++] = b;
          idx[o++] = b; idx[o++] = c; idx[o++] = d;
        }
      }
      return { v: v, i: idx };
    }

    function buildRibIndex() {
      var idx = new Uint16Array((CH_TENT * (TENT_NODES - 1) + CH_ARM * (ARM_NODES - 1)) * 6);
      var o = 0, c, k;
      for (c = 0; c < CHAINS; c++) {
        var base = chOff[c] * 2, len = chLen[c];
        for (k = 0; k < len - 1; k++) {
          var a = base + k * 2, b = a + 1, cc = a + 2, d = a + 3;
          idx[o++] = a; idx[o++] = b; idx[o++] = cc;
          idx[o++] = b; idx[o++] = d; idx[o++] = cc;
        }
      }
      return idx;
    }

    function staticBuf(target, data) {
      var b = gl.createBuffer();
      gl.bindBuffer(target, b);
      gl.bufferData(target, data, gl.STATIC_DRAW);
      return b;
    }

    /* Build/rebuild every GL resource. Returns false on ANY failure so the
       caller can route to the 2D fallback instead of a black screen. */
    function initGL() {
      progBack = makeProg(BACK_VS, BACK_FS, { a_p: 0 },
        ['u_shaft', 'u_activity', 'u_aspect', 'u_hero']);
      progBell = makeProg(BELL_VS, BELL_FS, { a_tp: 0 },
        ['u_vp', 'u_pos', 'u_m', 'u_pw', 'u_dim', 'u_vpw', 'u_floor', 'u_cam', 'u_glow', 'u_sg', 'u_ir', 'u_gp', 'u_fl', 'u_pass', 'u_mi']);
      progRib = makeProg(RIB_VS, RIB_FS, { a_pos: 0, a_aux: 1 },
        ['u_vp', 'u_dim', 'u_vpw', 'u_floor', 'u_wob']);
      progSil = makeProg(SIL_VS, SIL_FS, { a_tp: 0, a_i0: 2, a_i1: 3 },
        ['u_vp', 'u_cam', 'u_time', 'u_dim', 'u_vpw', 'u_fogD']);
      progShell = makeProg(SHELL_VS, SHELL_FS, { a_q: 0, a_i0: 2, a_i1: 3 },
        ['u_vp', 'u_right', 'u_up', 'u_dimD']);
      progGlow = makeProg(GLOW_VS, GLOW_FS, { a_q: 0 },
        ['u_vp', 'u_pos', 'u_right', 'u_up', 'u_size', 'u_color', 'u_int']);
      progAura = makeProg(GLOW_VS, AURA_FS, { a_q: 0 },
        ['u_vp', 'u_pos', 'u_right', 'u_up', 'u_size', 'u_int', 'u_pulse', 'u_floor', 'u_dimD', 'u_ray']);
      progMote = makeProg(MOTE_VS, MOTE_FS, { a_q: 0, a_i0: 2, a_i1: 3 },
        ['u_vp', 'u_right', 'u_up', 'u_hpos', 'u_time', 'u_hs', 'u_act',
         'u_rip', 'u_ripA', 'u_dim', 'u_vpw', 'u_floor']);
      progStar = makeProg(GLOW_VS, STAR_FS, { a_q: 0 },
        ['u_vp', 'u_pos', 'u_right', 'u_up', 'u_size', 'u_int', 'u_tw', 'u_floor', 'u_dimD']);
      if (!progBack || !progBell || !progRib || !progSil || !progShell || !progGlow || !progAura ||
          !progMote || !progStar) { return false; }

      var bell = buildBellGrid();
      var ribIdx = buildRibIndex();
      bellIdxCount = bell.i.length;
      ribIdxCount = ribIdx.length;
      quadBuf = staticBuf(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]));
      bellVBuf = staticBuf(gl.ARRAY_BUFFER, bell.v);
      bellIBuf = staticBuf(gl.ELEMENT_ARRAY_BUFFER, bell.i);
      ribIBuf = staticBuf(gl.ELEMENT_ARRAY_BUFFER, ribIdx);
      silBuf = staticBuf(gl.ARRAY_BUFFER, silF);
      moteBuf = staticBuf(gl.ARRAY_BUFFER, moteF);
      ribVBuf = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, ribVBuf);
      gl.bufferData(gl.ARRAY_BUFFER, ribF.byteLength, gl.DYNAMIC_DRAW);
      shellBuf = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, shellBuf);
      gl.bufferData(gl.ARRAY_BUFFER, shellF.byteLength, gl.DYNAMIC_DRAW);

      gl.disable(gl.DEPTH_TEST);
      gl.disable(gl.CULL_FACE);
      gl.clearColor(0.016, 0.039, 0.071, 1);
      if (gl.getError && gl.getError() !== gl.NO_ERROR && !gl.isContextLost()) { return false; }
      return true;
    }

    /* -------------------------------------------------------------- camera */
    function perspective(m, fovy, asp, near, far) {
      var f = 1 / Math.tan(fovy / 2), nf = 1 / (near - far);
      m[0] = f / asp; m[1] = 0; m[2] = 0; m[3] = 0;
      m[4] = 0; m[5] = f; m[6] = 0; m[7] = 0;
      m[8] = 0; m[9] = 0; m[10] = (far + near) * nf; m[11] = -1;
      m[12] = 0; m[13] = 0; m[14] = 2 * far * near * nf; m[15] = 0;
    }

    function mul4(o, a, b) {
      var c, r;
      for (c = 0; c < 4; c++) {
        for (r = 0; r < 4; r++) {
          o[c * 4 + r] = a[r] * b[c * 4] + a[4 + r] * b[c * 4 + 1] + a[8 + r] * b[c * 4 + 2] + a[12 + r] * b[c * 4 + 3];
        }
      }
    }

    function computeRay(nx, ny) {
      var vx = nx * tanXA, vy = ny * tanY;
      var dx = rgt[0] * vx + upv[0] * vy - bck[0];
      var dy = rgt[1] * vx + upv[1] * vy - bck[1];
      var dz = rgt[2] * vx + upv[2] * vy - bck[2];
      var il = 1 / Math.sqrt(dx * dx + dy * dy + dz * dz);
      urx = dx * il; ury = dy * il; urz = dz * il;
    }

    function updateCamera(dt) {
      var ty = mouseIn ? mouseNX * MAXTILT : 0;
      var tp = mouseIn ? mouseNY * MAXTILT : 0;
      yaw += (ty - yaw) * Math.min(1, dt * 2.2);
      pitch += (tp - pitch) * Math.min(1, dt * 2.2);
      var swy = yaw + Math.sin(timeS * 0.11) * 0.012;   /* idle sway */
      var swp = pitch + Math.cos(timeS * 0.14) * 0.009;
      eyeX = Math.sin(timeS * 0.13) * 0.45;
      eyeY = Math.cos(timeS * 0.17) * 0.3;
      eyeZ = 0;
      var cy = Math.cos(swy), sy = Math.sin(swy);
      var cp = Math.cos(swp), sp = Math.sin(swp);
      rgt[0] = cy; rgt[1] = 0; rgt[2] = -sy;
      upv[0] = sy * sp; upv[1] = cp; upv[2] = cy * sp;
      bck[0] = sy * cp; bck[1] = -sp; bck[2] = cy * cp;
      mView[0] = rgt[0]; mView[4] = rgt[1]; mView[8] = rgt[2];
      mView[12] = -(rgt[0] * eyeX + rgt[1] * eyeY + rgt[2] * eyeZ);
      mView[1] = upv[0]; mView[5] = upv[1]; mView[9] = upv[2];
      mView[13] = -(upv[0] * eyeX + upv[1] * eyeY + upv[2] * eyeZ);
      mView[2] = bck[0]; mView[6] = bck[1]; mView[10] = bck[2];
      mView[14] = -(bck[0] * eyeX + bck[1] * eyeY + bck[2] * eyeZ);
      mView[3] = 0; mView[7] = 0; mView[11] = 0; mView[15] = 1;
      mul4(mVP, mProj, mView);
      computeRay(mouseNX, mouseNY);
      rayDX = urx; rayDY = ury; rayDZ = urz;
    }

    /* -------------------------------------------------------- hero geometry */
    /* JS mirror of bellPt(t=1, phi): the margin ring the tentacles hang from */
    function bellMarginLocal(phi) {
      var wxm = pulsePhase - 2.2;
      var wsm = Math.sin(wxm);
      var ctr = wsm > 0 ? wsm * wsm * (0.55 + 0.45 * wsm) : 0;
      var r = 0.887362;               /* sin(2.05) */
      var y = -0.276370;              /* cos(2.05) * 0.60 */
      var lobe = Math.cos(8 * phi);
      r += 0.062 * lobe * r;
      y -= 0.075 * (0.5 + 0.5 * lobe);
      /* lappet flutter mirror (t=1 -> sc^3=1): roots stay glued to the rim */
      y += 0.015 * Math.sin(24 * phi + flutPh + 2 * lobe);
      r += 0.006 * Math.sin(24 * phi - flutPh + 1.1);
      r *= 1 - 0.22 * ctr;
      y -= 0.13 * ctr;
      mgX = r * Math.cos(phi); mgY = y; mgZ = r * Math.sin(phi);
    }

    /* bell model matrix: Rz(leanZ) * Rx(leanX) * scale, column-major */
    function buildM9() {
      var cx = Math.cos(leanX), sx = Math.sin(leanX);
      var cz = Math.cos(leanZ), sz = Math.sin(leanZ);
      var s = heroScale;
      m9[0] = cz * s; m9[1] = sz * s; m9[2] = 0;
      m9[3] = -sz * cx * s; m9[4] = cz * cx * s; m9[5] = sx * s;
      m9[6] = sz * sx * s; m9[7] = -cz * sx * s; m9[8] = cx * s;
      var inv = 1 / (s * s);
      m9i[0] = m9[0] * inv; m9i[1] = m9[3] * inv; m9i[2] = m9[6] * inv;
      m9i[3] = m9[1] * inv; m9i[4] = m9[4] * inv; m9i[5] = m9[7] * inv;
      m9i[6] = m9[2] * inv; m9i[7] = m9[5] * inv; m9i[8] = m9[8] * inv;
    }

    function setupHero() {
      var mobile = W <= 720;
      var pxPerWorld = H / (2 * tanY * (-HERO_Z));
      if (mobile) {
        /* The console card spans the full width here and is opaque — anchor
           the bell in the measured open-water band ABOVE it (same idea as the
           desktop .column probe) so the hero is never buried behind the card. */
        var panel = document.querySelector('.panel');
        var pTop = panel ? panel.getBoundingClientRect().top : H * 0.34;
        var band = Math.max(H * 0.14, Math.min(H * 0.55, pTop));
        /* fit: bell is ~0.94·s half-wide and ~1.10·s tall (apex +0.60, scalloped
           margin ≈ −0.50) in world units at HERO_Z */
        heroScale = Math.max(0.55, Math.min(1.5, Math.min(
          (W * 0.62) / (2 * pxPerWorld * 0.94),
          (band * 0.80) / (pxPerWorld * 1.10))));
        anchorPxX = W * 0.68;   /* right of the brand block in the header band */
        /* margin scallops (~0.48·s below the anchor) rest just above the card;
           the 0.72·s floor keeps the leaning, bobbing apex off the viewport top */
        anchorPxY = Math.max(pxPerWorld * heroScale * 0.72,
                             band - pxPerWorld * heroScale * 0.48 - 6);
        heroFloor = 0.55;
      } else {
        var col = document.querySelector('.column');
        var right = col ? col.getBoundingClientRect().right : W * 0.3;
        anchorPxX = (right + W) * 0.5; anchorPxY = H * 0.40;
        var avail = Math.max(240, W - right);
        heroScale = Math.max(1.3, Math.min(2.15, (avail * 0.72) / (2 * pxPerWorld)));
        heroFloor = 0;
      }
      moteCount = mobile ? 40 : N_MOTE;   /* fewer star-motes on small screens */
      var c;
      for (c = 0; c < CHAINS; c++) {
        var vary = chKind[c] === 0 ? 0.84 + 0.32 * (0.5 + 0.5 * Math.sin(c * 12.9898)) : 1;
        chSeg[c] = (chKind[c] === 0 ? TENT_LEN : ARM_LEN) * vary * heroScale / (chLen[c] - 1);
      }
    }

    function resetHeroNodes() {
      var nx = (anchorPxX / Math.max(1, W)) * 2 - 1;
      var ny = 1 - (anchorPxY / Math.max(1, H)) * 2;
      heroX = nx * tanXA * (-HERO_Z);
      heroY = ny * tanY * (-HERO_Z);
      heroVX = 0; heroVY = 0;
      heroPX = heroX; heroPY = heroY;
      leanX = 0.30; leanZ = 0;
      buildM9();
      var c, k;
      for (c = 0; c < CHAINS; c++) {
        var off = chOff[c], len = chLen[c];
        if (chKind[c] === 0) { bellMarginLocal(chPhi[c]); }
        else {
          mgX = Math.cos(chPhi[c]) * 0.26; mgY = -0.04; mgZ = Math.sin(chPhi[c]) * 0.26;
        }
        var rx = heroPX + m9[0] * mgX + m9[3] * mgY + m9[6] * mgZ;
        var ry = heroPY + m9[1] * mgX + m9[4] * mgY + m9[7] * mgZ;
        var rz = HERO_Z + m9[2] * mgX + m9[5] * mgY + m9[8] * mgZ;
        for (k = 0; k < len; k++) {
          var i3 = (off + k) * 3;
          ndPos[i3] = rx; ndPos[i3 + 1] = ry - chSeg[c] * k; ndPos[i3 + 2] = rz;
          ndPrev[i3] = ndPos[i3]; ndPrev[i3 + 1] = ndPos[i3 + 1]; ndPrev[i3 + 2] = ndPos[i3 + 2];
        }
      }
    }

    /* ------------------------------------------------------------ hero sim */
    function heroVerlet(dt) {
      var damp = Math.exp(-dt * 2.2);
      var dt2 = dt * dt;
      var wantStir = mouseIn && cursorA > 0.05;
      var c, k, i3, j3;
      for (c = 0; c < CHAINS; c++) {
        var off = chOff[c], len = chLen[c];
        var kind = chKind[c];
        if (kind === 0) { bellMarginLocal(chPhi[c]); }
        else {
          mgX = Math.cos(chPhi[c]) * 0.26; mgY = -0.04; mgZ = Math.sin(chPhi[c]) * 0.26;
        }
        i3 = off * 3;
        ndPos[i3] = heroPX + m9[0] * mgX + m9[3] * mgY + m9[6] * mgZ;
        ndPos[i3 + 1] = heroPY + m9[1] * mgX + m9[4] * mgY + m9[7] * mgZ;
        ndPos[i3 + 2] = HERO_Z + m9[2] * mgX + m9[5] * mgY + m9[8] * mgZ;
        ndPrev[i3] = ndPos[i3]; ndPrev[i3 + 1] = ndPos[i3 + 1]; ndPrev[i3 + 2] = ndPos[i3 + 2];
        var seed = chSeed[c];
        for (k = 1; k < len; k++) {
          i3 = (off + k) * 3;
          var x = ndPos[i3], y = ndPos[i3 + 1], z = ndPos[i3 + 2];
          var vx = (x - ndPrev[i3]) * damp;
          var vy = (y - ndPrev[i3 + 1]) * damp;
          var vz = (z - ndPrev[i3 + 2]) * damp;
          var u = k / (len - 1);
          /* gentle abyssal current + per-chain sway */
          var ax = Math.sin(timeS * 0.55 + u * 4.3 + seed) * 0.34 + Math.sin(timeS * 0.21 + seed * 2.0) * 0.15;
          var az = Math.cos(timeS * 0.47 + u * 3.6 + seed * 1.3) * 0.26;
          var ay = -1.35 * (kind === 0 ? 1.0 : 0.75);   /* slow, water-buoyant sink */
          if (kind === 1) { ax *= 1.8; az *= 1.8; }
          /* idle S-curves: a lazy sinusoid travels root->tip, phase offset
             per chain, so the tentacles wave even in a still frame */
          var sAmp = u * (kind === 0 ? 1.05 : 0.5) * (0.7 + 0.3 * Math.sin(seed * 3.7));
          ax += Math.sin(timeS * 0.85 - u * 5.6 + seed) * sAmp;
          az += Math.cos(timeS * 0.74 - u * 4.4 + seed * 1.7 + 1.3) * sAmp * 0.8;
          /* short-lived post-poke kink: high-frequency zigzag down the chain */
          if (kink > 0.01) {
            var kf = kink * 6.0 * u;
            ax += Math.sin(u * 14.0 - timeS * 21.0 + seed) * kf;
            az += Math.cos(u * 12.0 - timeS * 18.0 + seed * 1.7) * kf * 0.7;
          }
          if (wantStir) {
            /* nearby nodes stir/curl toward the pointer ray */
            var rx2 = x - eyeX, ry2 = y - eyeY, rz2 = z - eyeZ;
            var along = rx2 * rayDX + ry2 * rayDY + rz2 * rayDZ;
            if (along > 1) {
              var qx = rx2 - rayDX * along, qy = ry2 - rayDY * along, qz = rz2 - rayDZ * along;
              var q2 = qx * qx + qy * qy + qz * qz;
              var R = 1.7 + along * 0.10;
              if (q2 < R * R && q2 > 1e-4) {
                var qd = Math.sqrt(q2);
                var tt2 = 1 - qd / R;
                tt2 = tt2 * tt2 * (3 - 2 * tt2);
                var pull = 6.5 * tt2 * cursorA * u;
                ax -= qx / qd * pull; ay -= qy / qd * pull; az -= qz / qd * pull;
                var cxr = rayDY * qz - rayDZ * qy;
                var cyr = rayDZ * qx - rayDX * qz;
                var czr = rayDX * qy - rayDY * qx;
                var curl = 2.4 * tt2 * cursorA;
                ax += cxr / qd * curl; ay += cyr / qd * curl; az += czr / qd * curl;
              }
            }
          }
          ndPrev[i3] = x; ndPrev[i3 + 1] = y; ndPrev[i3 + 2] = z;
          ndPos[i3] = x + vx + ax * dt2;
          ndPos[i3 + 1] = y + vy + ay * dt2;
          ndPos[i3 + 2] = z + vz + az * dt2;
        }
        /* distance constraints: root is pinned, whip travels down the chain */
        var seg = chSeg[c];
        var it;
        for (it = 0; it < 3; it++) {
          for (k = 1; k < len; k++) {
            i3 = (off + k) * 3; j3 = (off + k - 1) * 3;
            var dx = ndPos[i3] - ndPos[j3];
            var dy = ndPos[i3 + 1] - ndPos[j3 + 1];
            var dz = ndPos[i3 + 2] - ndPos[j3 + 2];
            var d = Math.sqrt(dx * dx + dy * dy + dz * dz) || 1e-6;
            var diff = (d - seg) / d;
            if (k === 1) {
              ndPos[i3] -= dx * diff; ndPos[i3 + 1] -= dy * diff; ndPos[i3 + 2] -= dz * diff;
            } else {
              var hx = dx * diff * 0.5, hy = dy * diff * 0.5, hz = dz * diff * 0.5;
              ndPos[j3] += hx; ndPos[j3 + 1] += hy; ndPos[j3 + 2] += hz;
              ndPos[i3] -= hx; ndPos[i3 + 1] -= hy; ndPos[i3 + 2] -= hz;
            }
          }
        }
      }
    }

    /* camera-facing ribbon extrusion into the preallocated stream */
    function buildRibbons() {
      var o = 0, c, k;
      for (c = 0; c < CHAINS; c++) {
        var off = chOff[c], len = chLen[c], kind = chKind[c];
        for (k = 0; k < len; k++) {
          var i3 = (off + k) * 3;
          var k0 = k > 0 ? k - 1 : 0, k1 = k < len - 1 ? k + 1 : len - 1;
          var a3 = (off + k0) * 3, b3 = (off + k1) * 3;
          var dx = ndPos[b3] - ndPos[a3], dy = ndPos[b3 + 1] - ndPos[a3 + 1], dz = ndPos[b3 + 2] - ndPos[a3 + 2];
          var x = ndPos[i3], y = ndPos[i3 + 1], z = ndPos[i3 + 2];
          var vx = x - eyeX, vy = y - eyeY, vz = z - eyeZ;
          var sx = dy * vz - dz * vy, sy = dz * vx - dx * vz, sz = dx * vy - dy * vx;
          var sl = Math.sqrt(sx * sx + sy * sy + sz * sz);
          if (sl < 1e-5) { sx = 1; sy = 0; sz = 0; sl = 1; }
          sx /= sl; sy /= sl; sz /= sl;
          var u = k / (len - 1);
          var w, kv;
          if (kind === 0) {
            /* subtle beaded swell along the strand (thin ribbons stay thin) */
            w = heroScale * (0.030 - 0.020 * u + 0.0045 * Math.sin(u * 24.0 + chSeed[c]));
            kv = c * 0.002;   /* per-chain bead phase, still < 0.5 (tentacle branch) */
          } else {
            w = heroScale * 0.135 * (1 - 0.42 * u) *
              (1 + 0.36 * Math.sin(u * 12.0 + timeS * 1.5 + c * 1.9));
            kv = 1 + (c - CH_TENT) * 0.02;   /* de-syncs each arm's ruffle */
          }
          var br = heroBright * (kind === 0
            ? 0.80 + 0.36 * (0.5 + 0.5 * Math.sin(c * 5.7 + 1.3))
            : 1.30);
          ribF[o++] = x - sx * w; ribF[o++] = y - sy * w; ribF[o++] = z - sz * w;
          ribF[o++] = u; ribF[o++] = -1; ribF[o++] = br; ribF[o++] = kv;
          ribF[o++] = x + sx * w; ribF[o++] = y + sy * w; ribF[o++] = z + sz * w;
          ribF[o++] = u; ribF[o++] = 1; ribF[o++] = br; ribF[o++] = kv;
        }
      }
    }

    /* ----------------------------------------------------------------- sim */
    function sim(dt, nowMs) {
      activity += (activityTarget - activity) * Math.min(1, dt * 1.6);
      if (Math.abs(activityTarget - activity) < 0.002) { activity = activityTarget; }

      var wantCursor = mouseIn && (nowMs - lastMouseT < 2000);
      cursorA += ((wantCursor ? 1 : 0) - cursorA) * Math.min(1, dt * 4);

      updateCamera(dt);

      var si;
      for (si = 0; si < MAX_SHELL; si++) {
        var sh = shells[si];
        if (!sh.active) { continue; }
        if (sh.delay > 0) { sh.delay -= dt; continue; }
        sh.r += sh.speed * dt;
        var f = 1 - sh.r / sh.maxR;
        if (f <= 0) { sh.active = false; continue; }
        sh.alpha = f * f * Math.min(1, sh.strength);
      }
      if (flash.active) {
        flash.t += dt;
        if (flash.t >= flash.dur) { flash.active = false; }
      }

      /* the breath: traveling contraction, activity drives the rate */
      pulsePhase += (TWO_PI / BREATH) * (0.8 + 0.7 * activity) * dt;
      if (pulsePhase > TWO_PI) { pulsePhase -= TWO_PI; }
      flare *= Math.max(0, 1 - dt * 1.8);
      kink *= Math.max(0, 1 - dt * 1.9);
      /* click ripple sweeping the star-motes: radius expands, light decays
         (both bounded — the radius freezes once the amplitude dies) */
      if (ripA > 0) {
        ripR += 14 * dt;
        ripA *= Math.exp(-dt * 1.9);
        if (ripA < 0.01) { ripA = 0; ripR = 0; }
      }
      /* sample the traveling contraction wave at the gonad ring (t~=0.4,
         same math as bellPt's wx = u_pw - t * 2.2) — the clover breathes
         with the bell kick */
      var wsg = Math.sin(pulsePhase - 0.88);
      gonadPulse = wsg > 0 ? wsg * wsg * (0.55 + 0.45 * wsg) : 0;

      /* soft anchor spring toward the layout anchor (recomputed vs the
         swaying camera each frame, so the Medusa breathes in place) */
      var nx = (anchorPxX / Math.max(1, W)) * 2 - 1;
      var ny = 1 - (anchorPxY / Math.max(1, H)) * 2;
      computeRay(nx, ny);
      var tA = urz < -0.001 ? (HERO_Z - eyeZ) / urz : -HERO_Z;
      var tgx = eyeX + urx * tA, tgy = eyeY + ury * tA;
      computeRay(mouseNX, mouseNY);   /* restore the cursor scratch ray */
      heroVX += ((tgx - heroX) * 3.0 - heroVX * 2.4) * dt;
      heroVY += ((tgy - heroY) * 3.0 - heroVY * 2.4) * dt;
      heroX += heroVX * dt; heroY += heroVY * dt;
      /* slight upward surge as the power stroke reaches the margin */
      heroPX = heroX;
      heroPY = heroY + 0.07 * heroScale * Math.sin(pulsePhase - 3.3);

      /* bell lean: presentation tilt (dome face to the camera) + cursor + idle roll */
      var ltx = 0.30 + (mouseIn ? -mouseNY * 0.10 : 0) + Math.sin(timeS * 0.33) * 0.05;
      var ltz = (mouseIn ? mouseNX * 0.12 : 0) + Math.sin(timeS * 0.26 + 1.7) * 0.06 - heroVX * 0.06;
      leanX += (ltx - leanX) * Math.min(1, dt * 1.8);
      leanZ += (ltz - leanZ) * Math.min(1, dt * 1.8);
      buildM9();

      /* shading drivers (bounded uniforms; never raw unbounded time in FS) */
      shaftGlow = 0.5 + 0.25 * Math.sin(timeS * 0.31) + 0.25 * Math.sin(timeS * 0.23 + 2.1);
      iridPh = (timeS * 0.6) % TWO_PI;
      wobPh = (timeS * 1.6) % TWO_PI;
      flutPh = (timeS * 4.2) % TWO_PI;
      rayPh = (timeS * 0.23) % TWO_PI;
      starTw = (timeS * 2.7) % TWO_PI;
      var hdx = heroPX - eyeX, hdy = heroPY - eyeY, hdz = HERO_Z - eyeZ;
      var hDist = Math.sqrt(hdx * hdx + hdy * hdy + hdz * hdz);
      heroBright = Math.exp(-hDist * FOG_D) * (0.9 + 0.25 * activity) * (1 + 0.5 * Math.min(flare, 1.5));
      /* apex star: gentle multi-sine twinkle; the sonar bloom makes it flare
         hard, then it settles as the flare energy decays */
      starInt = heroBright * (0.42 + 0.10 * Math.sin(timeS * 2.1) + 0.07 * Math.sin(timeS * 3.7)) *
        (1 + 1.05 * Math.min(flare, 1.5));

      /* contrast staging: project the hero into backdrop uv space so the
         water directly behind the creature runs a stop darker (scalar math
         only — no allocation); wider + stronger than the aura's own channel */
      var hcw = mVP[3] * heroPX + mVP[7] * heroPY + mVP[11] * HERO_Z + mVP[15];
      if (hcw > 0.001) {
        heroU = (mVP[0] * heroPX + mVP[4] * heroPY + mVP[8] * HERO_Z + mVP[12]) / hcw * 0.5 + 0.5;
        heroV = (mVP[1] * heroPX + mVP[5] * heroPY + mVP[9] * HERO_Z + mVP[13]) / hcw * 0.5 + 0.5;
      }
      heroR = heroScale * 1.7 / (2 * tanY * hDist);
      heroDk = 0.38 * Math.min(1, heroBright + 0.3);

      heroVerlet(dt);
      buildRibbons();

      /* pack visible shells for the ring draw */
      shellCount = 0;
      for (si = 0; si < MAX_SHELL; si++) {
        var sh3 = shells[si];
        if (!sh3.active || sh3.delay > 0 || sh3.r <= 0.05) { continue; }
        var so = shellCount * 8;
        shellF[so] = sh3.x; shellF[so + 1] = sh3.y; shellF[so + 2] = sh3.z;
        shellF[so + 3] = sh3.r / 0.82;   /* quad radius so the ring peak sits at r */
        shellF[so + 4] = sh3.alpha * 0.9;
        shellF[so + 5] = 0; shellF[so + 6] = 0; shellF[so + 7] = 0;
        shellCount++;
      }
    }

    /* ---------------------------------------------------------------- draw */
    function bindQuad() {
      gl.bindBuffer(gl.ARRAY_BUFFER, quadBuf);
      gl.enableVertexAttribArray(0);
      gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
      setDiv(0, 0);
    }

    function unbindInstances() {
      setDiv(2, 0); gl.disableVertexAttribArray(2);
      setDiv(3, 0); gl.disableVertexAttribArray(3);
    }

    function drawGlowQuad(x, y, z, size, cr, cg, cb, intensity) {
      gl.useProgram(progGlow.p);
      gl.uniformMatrix4fv(progGlow.u_vp, false, mVP);
      gl.uniform3f(progGlow.u_pos, x, y, z);
      gl.uniform3f(progGlow.u_right, rgt[0], rgt[1], rgt[2]);
      gl.uniform3f(progGlow.u_up, upv[0], upv[1], upv[2]);
      gl.uniform1f(progGlow.u_size, size);
      gl.uniform3f(progGlow.u_color, cr, cg, cb);
      gl.uniform1f(progGlow.u_int, intensity);
      bindQuad();
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    }

    /* <=11 draw calls/frame (Ascension budget <=12): backdrop, cursor glow
       (cond), silhouettes, aura+aureole, star-motes, ribbons, bell back wall,
       bell front wall, apex star, shells (cond), flash (cond) */
    function draw() {
      gl.viewport(0, 0, canvas.width, canvas.height);

      /* backdrop: opaque gradient + animated light shafts */
      gl.disable(gl.BLEND);
      gl.useProgram(progBack.p);
      gl.uniform4f(progBack.u_shaft,
        Math.sin(timeS * 0.31), Math.sin(timeS * 0.23 + 2.1),
        Math.sin(timeS * 0.27 + 4.2), Math.sin(timeS * 0.19 + 1.1));
      gl.uniform1f(progBack.u_activity, activity);
      gl.uniform1f(progBack.u_aspect, aspect);
      gl.uniform4f(progBack.u_hero, heroU, heroV, heroR, heroDk);
      bindQuad();
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);

      gl.enable(gl.BLEND);
      gl.blendFunc(gl.ONE, gl.ONE);

      /* cursor light billboard at mid-depth along the pointer ray */
      if (cursorA > 0.01 && urz < -0.001) {
        var ct = (CURSOR_Z - eyeZ) / urz;
        drawGlowQuad(eyeX + urx * ct, eyeY + ury * ct, eyeZ + urz * ct,
          3.2 + activity * 1.0, 0.35, 0.62, 0.80, cursorA * 0.30);
      }

      /* distant silhouettes (one instanced draw, static instance buffer) */
      gl.useProgram(progSil.p);
      gl.uniformMatrix4fv(progSil.u_vp, false, mVP);
      gl.uniform3f(progSil.u_cam, eyeX, eyeY, eyeZ);
      gl.uniform1f(progSil.u_time, timeS);
      gl.uniform2f(progSil.u_dim, dimL, dimR);
      gl.uniform1f(progSil.u_vpw, W);
      gl.uniform1f(progSil.u_fogD, FOG_D);
      gl.bindBuffer(gl.ARRAY_BUFFER, bellVBuf);
      gl.enableVertexAttribArray(0);
      gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
      setDiv(0, 0);
      gl.bindBuffer(gl.ARRAY_BUFFER, silBuf);
      gl.enableVertexAttribArray(2);
      gl.vertexAttribPointer(2, 4, gl.FLOAT, false, 32, 0);
      setDiv(2, 1);
      gl.enableVertexAttribArray(3);
      gl.vertexAttribPointer(3, 4, gl.FLOAT, false, 32, 16);
      setDiv(3, 1);
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, bellIBuf);
      drawElemInst(bellIdxCount, N_SIL);
      unbindInstances();

      /* hero aura + contrast staging: glow lobes add, alpha darkens behind */
      gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      gl.useProgram(progAura.p);
      gl.uniformMatrix4fv(progAura.u_vp, false, mVP);
      gl.uniform3f(progAura.u_pos, heroPX, heroPY + 0.2 * heroScale, HERO_Z);
      gl.uniform3f(progAura.u_right, rgt[0], rgt[1], rgt[2]);
      gl.uniform3f(progAura.u_up, upv[0], upv[1], upv[2]);
      gl.uniform1f(progAura.u_size, heroScale * 3.1);
      gl.uniform1f(progAura.u_int, heroBright);
      gl.uniform1f(progAura.u_pulse, gonadPulse);
      gl.uniform1f(progAura.u_floor, heroFloor);
      gl.uniform3f(progAura.u_dimD, dimL * DPR, dimR * DPR, 120 * DPR);
      gl.uniform1f(progAura.u_ray, rayPh);
      bindQuad();
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      gl.blendFunc(gl.ONE, gl.ONE);

      /* ascending star-motes (one instanced draw, static instance buffer) */
      gl.useProgram(progMote.p);
      gl.uniformMatrix4fv(progMote.u_vp, false, mVP);
      gl.uniform3f(progMote.u_right, rgt[0], rgt[1], rgt[2]);
      gl.uniform3f(progMote.u_up, upv[0], upv[1], upv[2]);
      gl.uniform3f(progMote.u_hpos, heroPX, heroPY, HERO_Z);
      gl.uniform1f(progMote.u_time, timeS % MOTE_T);
      gl.uniform1f(progMote.u_hs, heroScale);
      gl.uniform1f(progMote.u_act, activity);
      gl.uniform4f(progMote.u_rip, ripX, ripY, ripZ, ripR);
      gl.uniform1f(progMote.u_ripA, ripA);
      gl.uniform2f(progMote.u_dim, dimL, dimR);
      gl.uniform1f(progMote.u_vpw, W);
      gl.uniform1f(progMote.u_floor, heroFloor);
      bindQuad();
      gl.bindBuffer(gl.ARRAY_BUFFER, moteBuf);
      gl.enableVertexAttribArray(2);
      gl.vertexAttribPointer(2, 4, gl.FLOAT, false, 32, 0);
      setDiv(2, 1);
      gl.enableVertexAttribArray(3);
      gl.vertexAttribPointer(3, 4, gl.FLOAT, false, 32, 16);
      setDiv(3, 1);
      drawArrInst(4, moteCount);
      unbindInstances();

      /* tentacles + oral arms: ONE dynamic upload, ONE draw */
      gl.useProgram(progRib.p);
      gl.uniformMatrix4fv(progRib.u_vp, false, mVP);
      gl.uniform2f(progRib.u_dim, dimL, dimR);
      gl.uniform1f(progRib.u_vpw, W);
      gl.uniform1f(progRib.u_floor, heroFloor);
      gl.uniform1f(progRib.u_wob, wobPh);
      gl.bindBuffer(gl.ARRAY_BUFFER, ribVBuf);
      gl.bufferSubData(gl.ARRAY_BUFFER, 0, ribF);
      gl.enableVertexAttribArray(0);
      gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 28, 0);
      setDiv(0, 0);
      gl.enableVertexAttribArray(1);
      gl.vertexAttribPointer(1, 4, gl.FLOAT, false, 28, 12);
      setDiv(1, 0);
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ribIBuf);
      gl.drawElements(gl.TRIANGLES, ribIdxCount, gl.UNSIGNED_SHORT, 0);
      gl.disableVertexAttribArray(1);

      /* the Medusa's bell: translucent body over the scene (premultiplied) */
      gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      gl.useProgram(progBell.p);
      gl.uniformMatrix4fv(progBell.u_vp, false, mVP);
      gl.uniform3f(progBell.u_pos, heroPX, heroPY, HERO_Z);
      gl.uniformMatrix3fv(progBell.u_m, false, m9);
      gl.uniform1f(progBell.u_pw, pulsePhase);
      gl.uniform2f(progBell.u_dim, dimL, dimR);
      gl.uniform1f(progBell.u_vpw, W);
      gl.uniform1f(progBell.u_floor, heroFloor);
      gl.uniform3f(progBell.u_cam, eyeX, eyeY, eyeZ);
      gl.uniform1f(progBell.u_glow, heroBright);
      gl.uniform1f(progBell.u_sg, shaftGlow);
      gl.uniform1f(progBell.u_ir, iridPh);
      gl.uniform1f(progBell.u_gp, gonadPulse);
      gl.uniform1f(progBell.u_fl, flutPh);
      gl.uniformMatrix3fv(progBell.u_mi, false, m9i);
      gl.bindBuffer(gl.ARRAY_BUFFER, bellVBuf);
      gl.enableVertexAttribArray(0);
      gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
      setDiv(0, 0);
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, bellIBuf);
      /* two-pass translucency: the far wall first, then the glassy near wall
         over it — the eye sees the inside of the back of the bell THROUGH
         the front, which is what makes it read as a volume of living jelly */
      gl.uniform1f(progBell.u_pass, 0);
      gl.drawElements(gl.TRIANGLES, bellIdxCount, gl.UNSIGNED_SHORT, 0);
      gl.uniform1f(progBell.u_pass, 1);
      gl.drawElements(gl.TRIANGLES, bellIdxCount, gl.UNSIGNED_SHORT, 0);
      gl.blendFunc(gl.ONE, gl.ONE);

      /* apex star-core: her nucleus elevated to a 4-point star (scalar math
         only — billboard at the upper interior of the bell, over the glass) */
      gl.useProgram(progStar.p);
      gl.uniformMatrix4fv(progStar.u_vp, false, mVP);
      gl.uniform3f(progStar.u_pos,
        heroPX + m9[3] * 0.34, heroPY + m9[4] * 0.34, HERO_Z + m9[5] * 0.34);
      gl.uniform3f(progStar.u_right, rgt[0], rgt[1], rgt[2]);
      gl.uniform3f(progStar.u_up, upv[0], upv[1], upv[2]);
      gl.uniform1f(progStar.u_size, heroScale * (0.52 + 0.20 * Math.min(flare, 1.5)));
      gl.uniform1f(progStar.u_int, starInt);
      gl.uniform1f(progStar.u_tw, starTw);
      gl.uniform1f(progStar.u_floor, heroFloor);
      gl.uniform3f(progStar.u_dimD, dimL * DPR, dimR * DPR, 120 * DPR);
      bindQuad();
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);

      /* shockwave shells (one instanced draw, <= 6 quads) */
      if (shellCount > 0) {
        gl.useProgram(progShell.p);
        gl.uniformMatrix4fv(progShell.u_vp, false, mVP);
        gl.uniform3f(progShell.u_right, rgt[0], rgt[1], rgt[2]);
        gl.uniform3f(progShell.u_up, upv[0], upv[1], upv[2]);
        gl.uniform3f(progShell.u_dimD, dimL * DPR, dimR * DPR, 120 * DPR);
        bindQuad();
        gl.bindBuffer(gl.ARRAY_BUFFER, shellBuf);
        gl.bufferSubData(gl.ARRAY_BUFFER, 0, shellF);
        gl.enableVertexAttribArray(2);
        gl.vertexAttribPointer(2, 4, gl.FLOAT, false, 32, 0);
        setDiv(2, 1);
        gl.enableVertexAttribArray(3);
        gl.vertexAttribPointer(3, 4, gl.FLOAT, false, 32, 16);
        setDiv(3, 1);
        drawArrInst(4, shellCount);
        unbindInstances();
      }

      /* sonar flash at the pulse origin */
      if (flash.active) {
        var ft = 1 - flash.t / flash.dur;
        drawGlowQuad(flash.x, flash.y, flash.z,
          3.0 + (1 - ft) * 4.5, 0.55, 0.83, 0.95, ft * ft * 0.85);
      }
    }

    /* ---------------------------------------------------------------- loop */
    function frame(t) {
      rafId = 0;
      if (!running) { return; }
      var dt = (t - lastT) / 1000;
      lastT = t;
      if (dt > 0.05) { dt = 0.05; }
      timeS += dt;
      if (dt > 0) { sim(dt, t); }
      draw();
      rafId = requestAnimationFrame(frame);
    }

    function start() {
      if (running || !mounted || dead || contextLost) { return; }
      running = true;
      lastT = performance.now();
      if (!rafId) { rafId = requestAnimationFrame(frame); }
    }

    function stop() {
      running = false;
      if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
    }

    /* ----------------------------------------------------------- mgmt/events */
    function measureColumn() {
      var col = document.querySelector('.column');
      if (col) {
        var r = col.getBoundingClientRect();
        dimL = r.left - 60;
        dimR = r.right;
      } else {
        dimL = 0; dimR = -1;   /* disabled */
      }
    }

    function applyResizeNow(force) {
      if (!canvas || !gl) { return; }
      var rect = canvas.getBoundingClientRect();
      var newW = rect.width || window.innerWidth;
      var newH = rect.height || window.innerHeight;
      var newDPR = Math.min(2, window.devicePixelRatio || 1);
      measureColumn();
      if (!force && newW === W && newH === H && newDPR === DPR) { return; }
      var oldW = W;
      W = newW; H = newH; DPR = newDPR;
      canvas.width = Math.max(1, Math.round(W * DPR));
      canvas.height = Math.max(1, Math.round(H * DPR));
      aspect = W / Math.max(1, H);
      tanXA = tanY * aspect;
      perspective(mProj, FOVY, aspect, NEARZ, FARZ);
      var wasMobile = oldW > 0 && oldW <= 720;
      var isMobile = W <= 720;
      setupHero();
      if (!heroInited || oldW <= 0 || wasMobile !== isMobile) {
        heroInited = true;
        resetHeroNodes();
      }
    }

    var resizeTimer = 0;
    function onResize() {
      if (resizeTimer) { clearTimeout(resizeTimer); }
      resizeTimer = setTimeout(function () { resizeTimer = 0; applyResizeNow(false); }, 150);
    }

    function onMouseMove(e) {
      mouseNX = (e.clientX / Math.max(1, W)) * 2 - 1;
      mouseNY = 1 - (e.clientY / Math.max(1, H)) * 2;
      mouseIn = true;
      lastMouseT = performance.now();
    }

    function onMouseOut(e) {
      if (!e.relatedTarget) { mouseIn = false; }
    }

    function onVisibility() {
      if (document.hidden) { stop(); }
      else { start(); }
    }

    function fatal() {
      dead = true;
      stop();
      if (typeof api.onFatal === 'function') { api.onFatal(); }
    }

    function onCtxLost(e) {
      e.preventDefault();
      contextLost = true;
      stop();
    }

    function onCtxRestored() {
      contextLost = false;
      if (!isGL2) {
        /* WebGL extension objects are invalidated by context loss: a stale
           ANGLE_instanced_arrays ext silently no-ops every instanced draw
           (no GL error). Re-fetch it before rebuilding resources; a null
           result means instancing is gone -> demote to the 2D fallback. */
        instExt = gl ? gl.getExtension('ANGLE_instanced_arrays') : null;
        if (!instExt) { fatal(); return; }
      }
      var ok = false;
      try { ok = initGL(); } catch (err) { ok = false; }
      if (!ok) { fatal(); return; }
      applyResizeNow(true);
      start();
    }

    /* ----------------------------------------------------------------- API */
    /* mount() returns false on ANY setup failure (no context, missing
       instancing extension on webgl1, shader compile/link failure) so the
       selector can fall back to the 2D renderer. */
    function mount(canvasEl) {
      if (dead || mounted) { return false; }
      if (!canvasEl || typeof canvasEl.getContext !== 'function') { return false; }
      try {
        canvas = canvasEl;
        var attrs = {
          alpha: false, antialias: true, depth: false, stencil: false,
          premultipliedAlpha: true, preserveDrawingBuffer: false,
          powerPreference: 'high-performance'
        };
        gl = canvas.getContext('webgl2', attrs);
        isGL2 = !!gl;
        if (!gl) {
          gl = canvas.getContext('webgl', attrs) || canvas.getContext('experimental-webgl', attrs);
          if (gl) {
            instExt = gl.getExtension('ANGLE_instanced_arrays');
            if (!instExt) { gl = null; canvas = null; return false; }
          }
        }
        if (!gl || gl.isContextLost()) { gl = null; canvas = null; return false; }
        if (!initGL()) { gl = null; canvas = null; return false; }
      } catch (err) {
        gl = null; canvas = null;
        return false;
      }

      canvas.addEventListener('webglcontextlost', onCtxLost, false);
      canvas.addEventListener('webglcontextrestored', onCtxRestored, false);
      window.addEventListener('mousemove', onMouseMove, { passive: true });
      window.addEventListener('mouseout', onMouseOut, { passive: true });
      window.addEventListener('resize', onResize);
      document.addEventListener('visibilitychange', onVisibility);

      mounted = true;
      contextLost = false;       /* a loss on a PREVIOUS canvas must not gate
                                    this fresh context (demote->repromote) */
      W = 0; H = 0;              /* force the first resize to apply */
      heroInited = false;
      applyResizeNow(true);
      updateCamera(0);
      start();
      return true;
    }

    function pulse(clientX, clientY, strength) {
      if (!mounted || dead || contextLost) { return; }
      var s = (typeof strength === 'number' && isFinite(strength)) ? strength : 1;
      var x = typeof clientX === 'number' ? clientX : W * 0.5;
      var y = typeof clientY === 'number' ? clientY : H * 0.5;
      var nx = (x / Math.max(1, W)) * 2 - 1;
      var ny = 1 - (y / Math.max(1, H)) * 2;
      computeRay(nx, ny);
      var t = urz < -0.001 ? (PULSE_Z - eyeZ) / urz : -PULSE_Z;
      var px = eyeX + urx * t, py = eyeY + ury * t, pz = eyeZ + urz * t;
      var rings = s >= 2 ? 3 : 1;
      var k;
      for (k = 0; k < rings; k++) {
        /* claim a free slot, else recycle the most-expired shell (cap 6) */
        var slot = null, oldest = null, oldestF = -1, si;
        for (si = 0; si < MAX_SHELL; si++) {
          var sh = shells[si];
          if (!sh.active) { slot = sh; break; }
          var fexp = sh.maxR > 0 ? sh.r / sh.maxR : 1;
          if (fexp > oldestF) { oldestF = fexp; oldest = sh; }
        }
        if (!slot) { slot = oldest; }
        slot.active = true;
        slot.x = px; slot.y = py; slot.z = pz;
        slot.r = 0;
        slot.maxR = (rings > 1 ? 42 : 15) * (0.75 + Math.min(s, 3) * 0.18);
        slot.speed = (rings > 1 ? 20 : 15) * (0.8 + Math.min(s, 3) * 0.12);
        slot.delay = k * 0.18;                     /* staggered ~180ms */
        slot.strength = Math.min(s, 3) * (1 - k * 0.22);
        slot.alpha = 0;
      }
      if (s >= 2) {
        flash.active = true;
        flash.x = px; flash.y = py; flash.z = pz;
        flash.t = 0;
      }

      /* the light answers first: a ripple of brightness through nearby motes */
      ripX = px; ripY = py; ripZ = pz;
      ripR = 0.6;
      ripA = Math.min(1, 0.4 + 0.28 * Math.min(s, 3));

      /* the Medusa answers: spring impulse + brightness flare + tentacle whip */
      var sc = Math.min(s, 3);
      flare = Math.min(1.5, flare + 0.45 * sc);
      kink = Math.min(1.2, kink + 0.45 * sc);   /* arms the zigzag whip term */
      var hdx = heroX - px, hdy = heroY - py;
      var hdd = Math.sqrt(hdx * hdx + hdy * hdy) + 0.001;
      var himp = sc * 0.55 / (1 + hdd * 0.22);
      heroVX += hdx / hdd * himp;
      heroVY += hdy / hdd * himp;
      var ci, kk;
      for (ci = 0; ci < CHAINS; ci++) {
        var off2 = chOff[ci], len2 = chLen[ci];
        for (kk = 1; kk < len2; kk++) {
          var q3 = (off2 + kk) * 3;
          var qx = ndPos[q3] - px, qy = ndPos[q3 + 1] - py, qz = ndPos[q3 + 2] - pz;
          var qd = Math.sqrt(qx * qx + qy * qy + qz * qz) + 0.001;
          var kick = sc * 0.19 * heroScale * (kk / (len2 - 1)) / (1 + qd * 0.18);
          ndPrev[q3] -= qx / qd * kick;
          ndPrev[q3 + 1] -= qy / qd * kick;
          ndPrev[q3 + 2] -= qz / qd * kick;
          /* alternating lateral component so the whip zigzags immediately
             instead of the whole chain translating as one */
          var zig = kick * 0.6 * Math.sin(kk * 2.4 + ci * 1.9);
          ndPrev[q3] -= (-qy / qd) * zig;
          ndPrev[q3 + 1] -= (qx / qd) * zig;
        }
      }
    }

    function setActivity(level) {
      var v = (typeof level === 'number' && isFinite(level)) ? level : 0;
      activityTarget = v < 0 ? 0 : (v > 1 ? 1 : v);
    }

    function detach() {
      stop();
      if (resizeTimer) { clearTimeout(resizeTimer); resizeTimer = 0; }
      if (mounted) {
        window.removeEventListener('mousemove', onMouseMove);
        window.removeEventListener('mouseout', onMouseOut);
        window.removeEventListener('resize', onResize);
        document.removeEventListener('visibilitychange', onVisibility);
        if (canvas) {
          canvas.removeEventListener('webglcontextlost', onCtxLost, false);
          canvas.removeEventListener('webglcontextrestored', onCtxRestored, false);
        }
      }
      mounted = false;
      canvas = null;
      gl = null;
      instExt = null;
    }

    return api;
  }

  /* =============================================================== selector == */
  /* Owns window.Jellyfield. Picks the renderer at mount, re-selects when
   * prefers-reduced-motion flips (both directions), and demotes 3D -> 2D when
   * the GL context dies for good. A canvas only ever yields ONE context kind,
   * so every swap replaces the canvas element with a fresh clone first. */
  var sel2d = null;
  var sel3d = null;
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

  function selTry3D() {
    if (!sel3d) {
      sel3d = createJellyfield3D();
      sel3d.onFatal = selOnFatal;
    }
    if (sel3d.mount(selCanvas)) {
      selActive = sel3d;
      selKind = '3d';
      sel3d.setActivity(selLastActivity);
      return true;
    }
    return false;
  }

  function selOnFatal() {
    /* 3D context died and could not be restored: demote to 2D for the session.
       The old canvas is locked to its dead WebGL context — swap in a fresh one. */
    if (selKind !== '3d') { return; }
    if (sel3d) { sel3d.detach(); }
    selFreshCanvas();
    selUse2D();
  }

  function selReduced() {
    return !!(selMql && selMql.matches);
  }

  function selOnMotionChange() {
    if (!selCanvas || !selKind) { return; }
    if (selReduced() && selKind === '3d') {
      /* 3D has no static mode: hand the field to the 2D renderer. */
      sel3d.detach();
      selFreshCanvas();
      selUse2D();
    } else if (!selReduced() && selKind === '2d') {
      /* motion allowed again: try to promote back to 3D. */
      sel2d.detach();
      selFreshCanvas();
      if (!selTry3D()) {
        selFreshCanvas();   /* the failed 3D attempt may have claimed a GL context */
        selUse2D();
      }
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
    if (!selTry3D()) {
      selFreshCanvas();   /* the failed 3D attempt may have claimed a GL context */
      selUse2D();
    }
  }

  function selPulse(clientX, clientY, strength) {
    if (selActive) { selActive.pulse(clientX, clientY, strength); }
  }

  function selSetActivity(level) {
    var v = (typeof level === 'number' && isFinite(level)) ? level : 0;
    selLastActivity = v < 0 ? 0 : (v > 1 ? 1 : v);
    if (selActive) { selActive.setActivity(level); }
  }

  window.Jellyfield = { mount: selMount, pulse: selPulse, setActivity: selSetActivity };
})();
