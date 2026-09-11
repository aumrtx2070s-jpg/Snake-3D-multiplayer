/* Snake 3D - browser client.
 *
 * The server is authoritative: we send turn intents and render whatever
 * world snapshot comes back over the WebSocket. Rendering is Three.js
 * (r128 UMD, loaded as the global `THREE`); the gameplay grid is flat 2D.
 */
'use strict';

/* ============================================================ networking */
var WS_URL = (location.protocol === 'https:' ? 'wss://' : 'ws://') +
             location.host + '/ws';

var ws = null;
var wsReady = false;
var pendingIntent = null;   // {type:'create'|'join', name, code} to send on open
var reconnectDelay = 500;

var me = null;              // my player id
var myName = '';
var roomCode = '';
var grid = { w: 32, h: 24 };
var snap = null;            // latest state message
var snapAt = 0;             // performance.now() when `snap` arrived
var tickMs = 83;            // server tick period, told to us on join
var prevHeads = {};         // playerId -> [x,y] head cell from the *previous* snapshot
var phase = 'lobby';       // 'lobby' | 'playing'
var shareDismissed = false;

function connect() {
  setStatus('กำลังเชื่อมต่อ...');
  ws = new WebSocket(WS_URL);

  ws.onopen = function () {
    wsReady = true;
    reconnectDelay = 500;
    setStatus('เชื่อมต่อแล้ว');
    if (pendingIntent) send(pendingIntent);
  };

  ws.onmessage = function (ev) {
    var msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    handle(msg);
  };

  ws.onclose = function () {
    wsReady = false;
    ws = null;
    if (phase === 'playing') {
      showToast('การเชื่อมต่อหลุด — กำลังต่อใหม่...');
      // try to slip straight back into the same room
      pendingIntent = { type: 'join', code: roomCode, name: myName };
    }
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 1.7, 8000);
  };

  ws.onerror = function () { try { ws.close(); } catch (e) {} };
}

function send(obj) {
  if (ws && wsReady) ws.send(JSON.stringify(obj));
}

function handle(msg) {
  if (msg.type === 'joined') {
    me = msg.id;
    roomCode = msg.code;
    grid.w = msg.w;
    grid.h = msg.h;
    tickMs = msg.tick_ms || tickMs;
    pendingIntent = null;
    snap = null;
    prevHeads = {};
    enterGame();
  } else if (msg.type === 'error') {
    showErr(msg.msg || 'เกิดข้อผิดพลาด');
    pendingIntent = null;
  } else if (msg.type === 'state') {
    // remember where every head was, so the render loop can glide it
    // smoothly into its new cell instead of snapping every tick
    if (snap) {
      for (var i = 0; i < snap.players.length; i++) {
        var pp = snap.players[i];
        if (pp.alive && pp.body.length) prevHeads[pp.id] = pp.body[0];
      }
    }
    snap = msg;
    snapAt = performance.now();
    if (phase === 'playing') {
      updateScoreboard();
      updateToast();
      updateShareCard();
    }
  }
}

/* ============================================================ lobby UI */
var $ = function (id) { return document.getElementById(id); };

var lobbyEl = $('lobby'), hudEl = $('hud'), hintEl = $('hint');
var nameInput = $('name'), codeInput = $('code');
var errEl = $('err'), statusEl = $('status');

function setStatus(t) { statusEl.textContent = t; }
function showErr(t) { errEl.textContent = t; errEl.hidden = false; }
function clearErr() { errEl.hidden = true; }

function currentName() {
  var n = (nameInput.value || '').trim();
  if (!n) n = 'Player' + Math.floor(Math.random() * 900 + 100);
  return n.slice(0, 16);
}

$('create').addEventListener('click', function () {
  clearErr();
  myName = currentName();
  pendingIntent = { type: 'create', name: myName };
  if (wsReady) send(pendingIntent);
  else setStatus('กำลังเชื่อมต่อ...');
});

$('join').addEventListener('click', function () {
  clearErr();
  var code = (codeInput.value || '').trim().toUpperCase();
  if (code.length !== 4) { showErr('รหัสห้องมี 4 ตัวอักษร'); return; }
  myName = currentName();
  pendingIntent = { type: 'join', code: code, name: myName };
  if (wsReady) send(pendingIntent);
  else setStatus('กำลังเชื่อมต่อ...');
});

codeInput.addEventListener('input', function () {
  codeInput.value = codeInput.value.toUpperCase().replace(/[^A-Z0-9]/g, '');
});
codeInput.addEventListener('keydown', function (e) {
  if (e.key === 'Enter') $('join').click();
});
nameInput.addEventListener('keydown', function (e) {
  if (e.key === 'Enter') $('create').click();
});

function copyText(t, btn) {
  var done = function () {
    if (!btn) return;
    var old = btn.textContent;
    btn.textContent = 'คัดลอกแล้ว';
    setTimeout(function () { btn.textContent = old; }, 1200);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(t).then(done, done);
  } else {
    var ta = document.createElement('textarea');
    ta.value = t; document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); } catch (e) {}
    document.body.removeChild(ta); done();
  }
}
$('copybtn').addEventListener('click', function () { copyText(roomCode, this); });
$('share-copy').addEventListener('click', function () { copyText(roomCode, this); });

function enterGame() {
  phase = 'playing';
  lobbyEl.classList.add('hidden');
  hudEl.hidden = false;
  hintEl.hidden = false;
  $('roomcode').textContent = roomCode;
  $('share-code').textContent = roomCode;
  shareDismissed = false;
  buildBoard();
}

/* ============================================================ HUD updates */
var scoreboardEl = $('scoreboard'), toastEl = $('toast'), shareEl = $('share');

function updateScoreboard() {
  var ps = snap.players.slice().sort(function (a, b) { return b.score - a.score; });
  var html = '';
  for (var i = 0; i < ps.length; i++) {
    var p = ps[i];
    html += '<li class="' + (p.id === me ? 'me ' : '') + (p.alive ? '' : 'dead') + '">' +
            '<span class="dot" style="background:' + p.color + '"></span>' +
            '<span class="nm">' + escapeHtml(p.name) + '</span>' +
            '<span class="sc">' + p.score + '</span></li>';
  }
  scoreboardEl.innerHTML = html;
}

function myPlayer() {
  if (!snap) return null;
  for (var i = 0; i < snap.players.length; i++)
    if (snap.players[i].id === me) return snap.players[i];
  return null;
}

function updateToast() {
  var p = myPlayer();
  if (p && !p.alive) {
    var s = Math.max(0, Math.ceil(p.respawn_in));
    showToast('โดนแล้ว! เกิดใหม่ใน ' + s + ' วินาที');
  } else {
    hideToast();
  }
}
function showToast(t) { toastEl.textContent = t; toastEl.hidden = false; }
function hideToast() { toastEl.hidden = true; }

function updateShareCard() {
  var solo = snap && snap.players.length < 2 && !shareDismissed;
  shareEl.hidden = !solo;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}

/* ============================================================ input */
var KEYMAP = {
  ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'left', ArrowRight: 'right',
  w: 'up', s: 'down', a: 'left', d: 'right',
  W: 'up', S: 'down', A: 'left', D: 'right'
};
var lastSent = 0;

window.addEventListener('keydown', function (e) {
  var tag = (e.target && e.target.tagName) || '';
  if (tag === 'INPUT' || tag === 'TEXTAREA') return;
  var dir = KEYMAP[e.key];
  if (!dir) return;
  e.preventDefault();
  if (phase !== 'playing') return;
  shareDismissed = true;
  updateShareCard();
  var now = performance.now();
  if (now - lastSent < 55) return;
  lastSent = now;
  send({ type: 'dir', dir: dir });
});

/* ============================================================ Three.js scene */
var BG = 0x0F1117;
var scene, camera, renderer;
var boardGroup = null;
var segGeo, appleGeo;
var segPool = [], applePool = [];
var clock;

function initThree() {
  scene = new THREE.Scene();
  scene.background = new THREE.Color(BG);

  camera = new THREE.PerspectiveCamera(
    55, window.innerWidth / window.innerHeight, 0.1, 1000);

  renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(window.innerWidth, window.innerHeight);
  if ('outputEncoding' in renderer) renderer.outputEncoding = THREE.sRGBEncoding;
  $('scene').appendChild(renderer.domElement);

  scene.add(new THREE.AmbientLight(0xffffff, 0.55));
  scene.add(new THREE.HemisphereLight(0x8faaff, 0x080810, 0.35));
  var sun = new THREE.DirectionalLight(0xffffff, 0.95);
  sun.position.set(-18, 40, 14);
  scene.add(sun);

  segGeo = new THREE.BoxGeometry(0.86, 0.86, 0.86);
  appleGeo = new THREE.SphereGeometry(0.42, 20, 16);

  clock = new THREE.Clock();
  window.addEventListener('resize', onResize);
  animate();
}

function onResize() {
  if (!renderer) return;
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
}

/* grid cell -> world position (board centred on the origin, Y is up) */
function wx(gx) { return gx - grid.w / 2 + 0.5; }
function wz(gy) { return gy - grid.h / 2 + 0.5; }

function buildBoard() {
  if (boardGroup) { scene.remove(boardGroup); disposeGroup(boardGroup); }
  boardGroup = new THREE.Group();

  var W = grid.w, H = grid.h;

  // ground
  var ground = new THREE.Mesh(
    new THREE.PlaneGeometry(W, H),
    new THREE.MeshStandardMaterial({ color: 0x171A23, roughness: 0.95, metalness: 0.0 })
  );
  ground.rotation.x = -Math.PI / 2;
  boardGroup.add(ground);

  // grid lines
  var pts = [];
  for (var x = 0; x <= W; x++) { pts.push(x - W / 2, 0.02, -H / 2, x - W / 2, 0.02, H / 2); }
  for (var y = 0; y <= H; y++) { pts.push(-W / 2, 0.02, y - H / 2, W / 2, 0.02, y - H / 2); }
  var lg = new THREE.BufferGeometry();
  lg.setAttribute('position', new THREE.Float32BufferAttribute(pts, 3));
  boardGroup.add(new THREE.LineSegments(
    lg, new THREE.LineBasicMaterial({ color: 0x20242F })));

  // perimeter walls
  var wallMat = new THREE.MeshStandardMaterial({ color: 0x3B4257, roughness: 0.7 });
  var t = 0.5, hgt = 0.9;
  var walls = [
    [W + t * 2, hgt, t, 0, hgt / 2, -H / 2 - t / 2],
    [W + t * 2, hgt, t, 0, hgt / 2, H / 2 + t / 2],
    [t, hgt, H + t * 2, -W / 2 - t / 2, hgt / 2, 0],
    [t, hgt, H + t * 2, W / 2 + t / 2, hgt / 2, 0]
  ];
  for (var i = 0; i < walls.length; i++) {
    var wct = walls[i];
    var m = new THREE.Mesh(new THREE.BoxGeometry(wct[0], wct[1], wct[2]), wallMat);
    m.position.set(wct[3], wct[4], wct[5]);
    boardGroup.add(m);
  }

  scene.add(boardGroup);

  // frame the camera to the board size
  var span = Math.max(W, H);
  camera.position.set(0, span * 0.86, H * 0.62 + span * 0.36);
  camera.lookAt(0, 0, H * 0.04);
}

function disposeGroup(g) {
  g.traverse(function (o) {
    if (o.geometry) o.geometry.dispose();
    if (o.material) {
      if (Array.isArray(o.material)) o.material.forEach(function (m) { m.dispose(); });
      else o.material.dispose();
    }
  });
}

/* pooled meshes -------------------------------------------------------- */
function getSeg(i) {
  if (!segPool[i]) {
    var m = new THREE.Mesh(segGeo, new THREE.MeshStandardMaterial({
      roughness: 0.45, metalness: 0.05
    }));
    segPool[i] = m; scene.add(m);
  }
  return segPool[i];
}
function getApple(i) {
  if (!applePool[i]) {
    var m = new THREE.Mesh(appleGeo, new THREE.MeshStandardMaterial({
      color: 0xEF5350, emissive: 0x330000, roughness: 0.3
    }));
    applePool[i] = m; scene.add(m);
  }
  return applePool[i];
}

var _c = new THREE.Color();

function renderWorld(dt, elapsed) {
  var used = 0;
  if (snap && phase === 'playing') {
    // fraction of the way from the previous tick to this one, right now -
    // only the head actually needs this: every other body cell is a static
    // grid square between ticks (the array just shifts), so lerping it
    // would be a no-op anyway. this alone turns the 15Hz tick into
    // something that reads as smooth motion instead of a step every ~66ms.
    var t = Math.min(1, (performance.now() - snapAt) / tickMs);

    for (var pi = 0; pi < snap.players.length; pi++) {
      var p = snap.players[pi];
      if (!p.alive || !p.body.length) continue;
      _c.set(p.color);
      for (var bi = 0; bi < p.body.length; bi++) {
        var seg = getSeg(used++);
        seg.visible = true;
        var cell = p.body[bi];
        var px = cell[0], py = cell[1];
        if (bi === 0) {
          var prev = prevHeads[p.id];
          // a respawn teleports the head more than one cell away - never
          // glide across the whole board for that, just cut instantly
          if (prev && Math.abs(prev[0] - px) + Math.abs(prev[1] - py) <= 1) {
            px = prev[0] + (px - prev[0]) * t;
            py = prev[1] + (py - prev[1]) * t;
          }
        }
        seg.position.set(wx(px), 0.45, wz(py));
        var head = bi === 0;
        var s = head ? 1.12 : 0.9 - Math.min(bi, 12) * 0.012;
        seg.scale.set(s, s, s);
        seg.material.color.copy(_c);
        seg.material.emissive.copy(_c).multiplyScalar(head ? 0.35 : 0.12);
      }
    }
  }
  for (var k = used; k < segPool.length; k++) segPool[k].visible = false;

  var na = (snap && phase === 'playing') ? snap.apples.length : 0;
  for (var ai = 0; ai < na; ai++) {
    var a = getApple(ai);
    a.visible = true;
    a.position.set(wx(snap.apples[ai][0]),
                   0.5 + Math.sin(elapsed * 3 + ai) * 0.12,
                   wz(snap.apples[ai][1]));
    a.rotation.y += dt * 1.6;
  }
  for (var m = na; m < applePool.length; m++) applePool[m].visible = false;
}

function animate() {
  requestAnimationFrame(animate);
  var dt = clock.getDelta();
  var elapsed = clock.elapsedTime;
  renderWorld(dt, elapsed);
  renderer.render(scene, camera);
}

/* ============================================================ boot */
initThree();
connect();
