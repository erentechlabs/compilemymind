(() => {
  const diagrams = ".mermaid";
  let renderQueue = Promise.resolve();

  const activeTheme = () =>
    document.documentElement.getAttribute("data-theme") === "light" ? "base" : "dark";

  const renderDiagrams = () => {
    document.querySelectorAll(diagrams).forEach((diagram) => {
      if (!diagram.dataset.mermaidSource) {
        diagram.dataset.mermaidSource = diagram.innerHTML.trim();
      } else {
        diagram.innerHTML = diagram.dataset.mermaidSource;
      }
      diagram.removeAttribute("data-processed");
    });

    mermaid.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      theme: activeTheme(),
      fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif",
      flowchart: { htmlLabels: true, useMaxWidth: true }
    });

    return mermaid.run({ querySelector: diagrams });
  };

  const scheduleRender = () => {
    renderQueue = renderQueue.then(renderDiagrams).catch((error) => {
      console.error("Unable to render InfraLab diagrams", error);
    });
  };

  window.addEventListener("DOMContentLoaded", scheduleRender, { once: true });

  new MutationObserver((mutations) => {
    if (mutations.some((mutation) => mutation.attributeName === "data-theme")) {
      scheduleRender();
    }
  }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
})();
