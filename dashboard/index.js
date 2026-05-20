/**
 * Cronobs — Dashboard Plugin
 *
 * Embeds the Cron Observatory (port 8700) inside the Hermes Dashboard
 * via an iframe. Zero build step — plain IIFE using the host SDK.
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;

  const { React } = SDK;
  const h = React.createElement;
  const { useState, useEffect, useCallback, useRef } = SDK.hooks;

  const CRONOBS_URL = "http://127.0.0.1:8700";

  function CronobsPage() {
    const iframeRef = useRef(null);
    const [status, setStatus] = useState("loading"); // loading | ready | error

    const handleLoad = useCallback(() => {
      setStatus("ready");
    }, []);

    const handleError = useCallback(() => {
      setStatus("error");
    }, []);

    // Check if cronobs server is reachable before embedding
    useEffect(() => {
      let cancelled = false;
      fetch(CRONOBS_URL, { mode: "no-cors" })
        .then(() => {
          if (!cancelled) setStatus("ready");
        })
        .catch(() => {
          if (!cancelled) setStatus("error");
        });
      return () => { cancelled = true; };
    }, []);

    if (status === "error") {
      return h("div", {
        style: {
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          height: "100%",
          minHeight: "400px",
          gap: "16px",
          color: "var(--text-secondary, #888)",
          fontFamily: "'Space Grotesk', sans-serif",
        }
      },
        h("div", {
          style: { fontSize: "48px", opacity: 0.4 }
        }, "⏳"),
        h("div", {
          style: { fontSize: "16px", fontWeight: 600 }
        }, "Cronobs não está rodando"),
        h("div", {
          style: { fontSize: "13px", opacity: 0.7, textAlign: "center", maxWidth: "400px" }
        }, "Inicie o servidor com: hermes cronobs start"),
        h("button", {
          onClick: () => window.open(CRONOBS_URL, "_blank"),
          style: {
            marginTop: "8px",
            padding: "8px 16px",
            border: "1px solid var(--border, #333)",
            borderRadius: "6px",
            background: "transparent",
            color: "var(--text-primary, #ccc)",
            cursor: "pointer",
            fontSize: "13px",
            fontFamily: "'Space Mono', monospace",
          }
        }, "Abrir em nova aba ↗")
      );
    }

    return h("div", {
      style: {
        width: "100%",
        height: "100%",
        minHeight: "calc(100vh - 120px)",
        position: "relative",
      }
    },
      status === "loading" && h("div", {
        style: {
          position: "absolute",
          top: "50%",
          left: "50%",
          transform: "translate(-50%, -50%)",
          color: "var(--text-secondary, #888)",
          fontFamily: "'Space Mono', monospace",
          fontSize: "13px",
        }
      }, "Carregando Cronobs..."),
      h("iframe", {
        ref: iframeRef,
        src: CRONOBS_URL,
        onLoad: handleLoad,
        onError: handleError,
        style: {
          width: "100%",
          height: "100%",
          minHeight: "calc(100vh - 120px)",
          border: "none",
          borderRadius: "0",
          display: status === "ready" ? "block" : "none",
        },
        title: "Cron Observatory",
        sandbox: "allow-same-origin allow-scripts allow-popups",
      })
    );
  }

  // Register with the dashboard plugin system
  window.__HERMES_PLUGINS__.register("cronobs", CronobsPage);
})();
