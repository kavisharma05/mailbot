import { createRouter, createWebHistory } from 'vue-router'
import HomeView from '../views/HomeView.vue'
import TasksView from '../views/TasksView.vue'
import ChatView from '../views/ChatView.vue'
import ScheduleView from '../views/ScheduleView.vue'
import LibraryView from '../views/LibraryView.vue'
import LoginView from '../views/LoginView.vue'
import { useAuthStore } from '../stores/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: LoginView, meta: { public: true } },
    { path: '/', name: 'home', component: HomeView },
    { path: '/tasks', name: 'tasks', component: TasksView },
    { path: '/chat', name: 'chat', component: ChatView },
    { path: '/schedule', name: 'schedule', component: ScheduleView },
    { path: '/library', name: 'library', component: LibraryView },
    { path: '/settings', name: 'settings', redirect: '/' },
  ],
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (!auth.loaded) {
    await auth.bootstrap()
  }
  if (to.meta.public) {
    if (auth.isLoggedIn && to.name === 'login') {
      return '/'
    }
    return true
  }
  if (!auth.isLoggedIn) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  return true
})

export default router
