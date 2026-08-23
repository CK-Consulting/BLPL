// The diagram example is imported as text so a test can check it parses without
// pulling Node's fs types into a browser build.
declare module "*.mmd?raw" {
  const content: string;
  export default content;
}
