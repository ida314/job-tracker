// Mounts the toolbar into a page that has no React of its own. overlay.py names the
// annotation server on the script tag, so the port is set in one place.
import { createRoot } from "react-dom/client";
import { Agentation } from "agentation";

const { endpoint } = document.currentScript.dataset;
const host = document.createElement("div");
host.id = "agentation-overlay";
document.body.appendChild(host);
createRoot(host).render(<Agentation endpoint={endpoint} />);
