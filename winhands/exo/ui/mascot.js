// Placeholder mascot until the Claude Design character lands. API: setState(s), setLevel(0..1).
window.ExoMascot = (() => {
  const el = document.getElementById("mascot");
  el.innerHTML = `<svg viewBox="0 0 200 180" width="190"><ellipse cx="100" cy="110" rx="70" ry="55" fill="#8dc63f"/>
    <rect x="45" y="60" width="110" height="34" rx="14" fill="#0b1c16"/><rect id="visor" x="58" y="72" width="84" height="10" rx="5" fill="#a0d152"/></svg>`;
  return { setState(s) { el.dataset.state = s; }, setLevel(l) { el.querySelector("#visor").style.opacity = 0.4 + l * 0.6; } };
})();
