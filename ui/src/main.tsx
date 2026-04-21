import { render } from "solid-js/web";
import { Router, Route } from "@solidjs/router";
import App from "./App";
import ProjectList from "./views/ProjectList";
import ProjectDetail from "./views/ProjectDetail";
import "./styles.css";

const root = document.getElementById("root");
if (!root) throw new Error("#root element missing in index.html");

render(
  () => (
    <Router root={App}>
      <Route path="/" component={ProjectList} />
      <Route path="/projects/:id" component={ProjectDetail} />
    </Router>
  ),
  root,
);
