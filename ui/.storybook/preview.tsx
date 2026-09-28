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
      // 'error' — an axe violation FAILS the story's test, locally and in CI.
      //
      // This was 'todo' (warning-only) until agent-forge-harness-27h. In that
      // mode the addon guards its own `expect(...).toHaveNoViolations()` out of
      // existence: axe still runs and still records violations, the suite still
      // goes green, and four real violations sat behind it for weeks.
      //
      // What this gate does NOT prove is written down in ui/README.md, under
      // "What the axe gate does not check". Axe is a floor, not a ceiling.
      test: 'error',
    },
  },
}

export default preview
