import type { ParentComponent } from "solid-js";
import { A } from "@solidjs/router";

const App: ParentComponent = (props) => {
  return (
    <div class="app-shell">
      <header class="app-header">
        <A href="/" class="logo">
          <pre class="logo-grid">
{`┌───┬───┐
│ B │ L │
├───┼───┤
│ P │ L │
└───┴───┘`}
          </pre>
          <span class="logo-text">Board Layer Pipe Line</span>
        </A>
      </header>
      <main class="app-main">{props.children}</main>
      <footer class="app-footer">
        <span>BLPL v0.3.0 · local</span>
      </footer>
    </div>
  );
};

export default App;
