import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { HelmetProvider } from 'react-helmet-async'
import { initSentry, Sentry } from './lib/sentry'
import { initAnalytics } from './lib/analytics'
import { installClientErrorReporter } from './lib/clientErrorReporter'
import './index.css'
import App from './App.tsx'
import { I18nProvider } from './i18n/index'

initSentry()
initAnalytics()
installClientErrorReporter()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Sentry.ErrorBoundary fallback={<FallbackError />}>
      <HelmetProvider>
        <I18nProvider>
          <App />
        </I18nProvider>
      </HelmetProvider>
    </Sentry.ErrorBoundary>
  </StrictMode>
)

// Signal to the build-time prerenderer (`@prerenderer/rollup-plugin`) that the
// app has mounted and helmet tags are flushed. Has no effect in real browsers.
// Fire on the next animation frame AND again on a short timeout so the event
// reaches the prerenderer even if React's first paint is delayed by suspense
// or by hooks waiting on /api/* (which 404 against the prerender static server).
const fireRenderEvent = () => {
  document.dispatchEvent(new Event('render-event'))
}
requestAnimationFrame(fireRenderEvent)
setTimeout(fireRenderEvent, 500)

function FallbackError() {
  return (
    <div className="flex items-center justify-center h-screen bg-zinc-50">
      <div className="text-center space-y-4 max-w-md">
        <img src="/hero-logo.svg" alt="MCP Hero" className="h-20 w-auto mx-auto" />
        <h1 className="text-xl font-semibold text-zinc-900">Something went wrong</h1>
        <p className="text-sm text-zinc-500">
          The error has been reported. Try reloading the page.
        </p>
        <button
          onClick={() => window.location.reload()}
          className="px-4 py-2 text-sm bg-zinc-900 text-white rounded hover:bg-zinc-800"
        >
          Reload
        </button>
      </div>
    </div>
  )
}
