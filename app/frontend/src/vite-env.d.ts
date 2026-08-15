/// <reference types="vite/client" />

// tsconfig sets `"types": []` on purpose — it keeps stray @types packages out of
// the global scope. That also excludes Vite's own client types, which is why
// `import.meta.env` is not otherwise known here. This reference pulls in exactly
// the one we need, and nothing else.
