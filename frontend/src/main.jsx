import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App.jsx";
// Shell, brand and landing styles first; the workspace layer is loaded
// after so that where the two define the same selector, the workspace wins.
import "./styles.css";
import "./workspace.css";

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
