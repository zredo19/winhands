// Mounts the Claude Design character (exo-mascot.js) and plays the transform-in on start.
(() => {
  const el = document.getElementById("mascot");
  ExoMascot.mount(el, { size: 190, theme: "dark", fold: 1 });
  setTimeout(() => ExoMascot.transformIn(1600), 300);
})();
