import { tableCampaign } from './address'
import { mountTable } from './table'
import './table.css'

const stop = mountTable(document, tableCampaign(window.location, window.history))
if (import.meta.hot) import.meta.hot.dispose(stop)
