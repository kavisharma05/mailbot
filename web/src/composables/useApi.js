import axios from 'axios'

const api = axios.create({
  baseURL: '',
  timeout: 25000,
  headers: { 'Content-Type': 'application/json' },
})

export function useApi() {
  return api
}

export default api
