import type { Decorator, Preview } from '@storybook/react-vite'

// Aetheril design-system tokens: fonts → colors → typography → shape
// → elevation → spacing → motion → base (incl. .aether-parchment helper).
// Same global entry point the app loads in src/main.tsx.
import '../src/ds/styles.css'

/**
 * Theme decorator — mirrors ds/theme.tsx: Light Parchment is the :root
 * default; Dark Tavern sets data-theme="dark" on <html>. The parchment
 * ground is applied to <body> exactly as src/main.tsx does.
 */
const withAetherilTheme: Decorator = (Story, context) => {
  const theme = context.globals.theme
  if (theme === 'dark') {
    document.documentElement.setAttribute('data-theme', 'dark')
  } else {
    document.documentElement.removeAttribute('data-theme')
  }
  document.body.classList.add('aether-parchment')
  return <Story />
}

const preview: Preview = {
  globalTypes: {
    theme: {
      description: 'Aetheril color theme',
      toolbar: {
        title: 'Theme',
        icon: 'mirror',
        items: [
          { value: 'light', title: 'Light Parchment' },
          { value: 'dark', title: 'Dark Tavern' },
        ],
        dynamicTitle: true,
      },
    },
  },
  initialGlobals: {
    theme: 'light',
  },
  decorators: [withAetherilTheme],
  parameters: {
    layout: 'centered',
    controls: {
      matchers: {
        color: /(background|color)$/i,
        date: /Date$/i,
      },
    },

    a11y: {
      // 'todo' - show a11y violations in the test UI only
      // 'error' - fail CI on a11y violations
      // 'off' - skip a11y checks entirely
      test: 'todo',
    },
  },
}

export default preview
