import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import "@fontsource/zcool-kuaile/400.css";
import "@fontsource-variable/nunito/wght.css";
import App from "./App";
import { installGlassGlow } from "./glass";
import { ThemeProvider } from "./theme";
import "./styles.css";
import "./interaction.css";
import "./layout.css";
import "./design-system.css";
import "./agent-workspace/agent-workspace.css";
import "./agent-debug/debug-console.css";
import "./agent-prompts/prompts.css";
import "./relations/relations.css";
import "./environment.css";
import "./sandbox/sandbox.css";
import "./web-search/web-search.css";
import "./stickers/stickers.css";
import "./schedules/schedules.css";
import "./private-agent/private-agent.css";
import "./directory/directory.css";
import "./werewolf/werewolf.css";
import "./theme-anime.css";
import "./theme-dark.css";

installGlassGlow();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ThemeProvider>
      <BrowserRouter basename="/webui">
        <App />
      </BrowserRouter>
    </ThemeProvider>
  </React.StrictMode>,
);
