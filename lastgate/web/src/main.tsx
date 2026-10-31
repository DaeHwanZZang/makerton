import React from "react";
import ReactDOM from "react-dom/client";
import Launcher from "./apps/Launcher";
import InputApp from "./apps/input/InputApp";
import OutputApp from "./apps/output/OutputApp";
import "./shared/styles.css";

// /input → 입력 키오스크, /output → 출력 키오스크, 그 외 → 시작 화면
const path = window.location.pathname.replace(/\/+$/, "");
const Page = path === "/input" ? InputApp : path === "/output" ? OutputApp : Launcher;

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Page />
  </React.StrictMode>,
);
