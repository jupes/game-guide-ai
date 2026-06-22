import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
// Aetheril design-system tokens (CP-F1.1): fonts → colors → typography → shape
// → elevation → spacing → motion → base (incl. .aether-parchment helper)
import './ds/styles.css'
import './index.css'
import App from './App.tsx'
import { ThemeProvider } from './ds/theme'
import { AppNavProvider } from './shell/AppNav'
import { CurrentUserProvider } from './shell/currentUser'

// Apply the parchment ground to the document body so the app sits on parchment
// before React hydrates. This avoids a flash of the browser default background.
document.body.classList.add('aether-parchment')

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ThemeProvider>
      <AppNavProvider>
        <CurrentUserProvider>
          <App />
        </CurrentUserProvider>
      </AppNavProvider>
    </ThemeProvider>
  </StrictMode>,
)
