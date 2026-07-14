// <ecad-viewer> is a custom element from the vendored bundle, not a React
// component, so JSX needs to be told it exists.
declare namespace JSX {
  interface IntrinsicElements {
    "ecad-viewer": React.DetailedHTMLProps<
      React.HTMLAttributes<HTMLElement> & {
        "show-header"?: string;
        "header-sections"?: string;
      },
      HTMLElement
    >;
  }
}
