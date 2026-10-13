// Mounts the Claude Design character (exo-mascot.js) and plays the transform-in on start.
(() => {
  const el = document.getElementById("mascot");
  ExoMascot.mount(el, { size: 190, theme: "dark", fold: 1 });
  setTimeout(() => ExoMascot.transformIn(1600), 300);

  // Frame limiter: every repaint of a translucent window costs CPU, and CSS animations run at 60 fps.
  // Pause the character's animations and step them by hand: 12 fps for the slow idle / sleeping motion, 24 fps otherwise.
  const CALM = new Set(["idle", "sleeping"]);
  let last = performance.now();
  (function tick() {
    const now = performance.now(), dt = now - last;
    last = now;
    for (const a of el.getAnimations({ subtree: true })) {  // only the character: UI animations stay native
      if (a.playState === "running") a.pause();
      a.currentTime = (a.currentTime || 0) + dt;
    }
    setTimeout(tick, 1000 / (CALM.has(document.body.dataset.state) ? 12 : 24));
  })();

  // Every animation frame leaves garbage that a quiet page never collects (+18 MB/min, measured): collect it.
  setInterval(() => window.gc && gc(), 10000);  // gc exists with --js-flags=--expose-gc (app.py)
})();
