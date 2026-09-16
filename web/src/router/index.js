import { createRouter, createWebHistory } from 'vue-router'
import HomeView from '../views/HomeView.vue'
import TasksView from '../views/TasksView.vue'
import ChatView from '../views/ChatView.vue'
import ScheduleView from '../views/ScheduleView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'home', component: HomeView },
    { path: '/tasks', name: 'tasks', component: TasksView },
    { path: '/chat', name: 'chat', component: ChatView },
    { path: '/schedule', name: 'schedule', component: ScheduleView },
    { path: '/settings', name: 'settings', redirect: '/' },
  ],
})

export default router
