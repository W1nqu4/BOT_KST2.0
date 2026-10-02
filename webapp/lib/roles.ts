/**
 * Подписи и права ролей.
 *
 * Роли приходят с бэкенда (`/api/profile`): `students.role` плюс `admin` для
 * id из `ADMIN_IDS`. Здесь только отображение и проверка прав на фронте —
 * настоящая проверка прав всегда на сервере.
 */

import type { Role } from './api-types'

export const ROLE_LABEL: Record<Role, string> = {
  student: 'Студент',
  deputy: 'Зам. старосты',
  starosta: 'Староста',
  admin: 'Админ',
}

/** Что доступно роли в интерфейсе. */
export function permissions(role: Role) {
  return {
    /** Отметки явки: староста, зам и админ. */
    manageAttendance: role !== 'student',
    /** Управление группой: только староста и админ. */
    manageGroup: role === 'starosta' || role === 'admin',
    isAdmin: role === 'admin',
  }
}