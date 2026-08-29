/**
 * 2026-08-29：自制的最小 Pinia 持久化插件，替换 `pinia-plugin-persistedstate`
 *（GitHub 仓库已被官方标记为 archived，停止维护——见依赖审计记录）。
 *
 * 全仓库对这个插件的实际使用只有一处：`stores/app.ts` 的 `collapsed` 一个
 * 布尔字段（侧边栏是否折叠），没有涉及嵌套路径、自定义 storage、序列化器等
 * 复杂选项。没有理由为了这么小的需求继续依赖一个没人维护的第三方包——
 * 写一个只做这一件事的插件，比引入另一个候选依赖（同样有"它以后会不会也
 * 停更"的问题）更简单，也彻底消除了这一条依赖风险。
 *
 * 用法与原插件的 `persist: { pick: [...] }` 形状保持一致，`stores/app.ts`
 * 不需要跟着改：
 *   defineStore('app', () => {...}, { persist: { pick: ['collapsed'] } })
 */
import type { PiniaPluginContext, StateTree } from 'pinia'

interface PersistOptions {
  /** 只持久化这些字段；省略则不做任何持久化（保持显式，不做"默认全量持久化"）。 */
  pick?: string[]
}

declare module 'pinia' {
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  export interface DefineStoreOptionsBase<S, Store> {
    persist?: PersistOptions
  }
}

const STORAGE_PREFIX = 'pinia_'

export function persistPlugin({ store, options }: PiniaPluginContext): void {
  const persist = options.persist
  if (!persist?.pick?.length) return

  const storageKey = STORAGE_PREFIX + store.$id
  const pick = persist.pick

  // 启动时恢复：只读取声明了的字段，其余字段保持 store 自身的初始值不受影响。
  try {
    const raw = localStorage.getItem(storageKey)
    if (raw) {
      const saved = JSON.parse(raw) as Record<string, unknown>
      const patch: Record<string, unknown> = {}
      for (const key of pick) {
        if (key in saved) patch[key] = saved[key]
      }
      // $patch's typed overloads want the store's own concrete state shape,
      // which a generic cross-store plugin can't know — same reason
      // pinia-plugin-persistedstate's own internals do the same narrowing.
      if (Object.keys(patch).length) store.$patch(patch as Partial<StateTree>)
    }
  } catch {
    // 存量数据损坏/格式不对：当作没有持久化数据处理，不阻断启动，不抛错。
  }

  // 每次变更后：只写回声明了的字段，避免把整个 store（含运行时/敏感字段）
  // 意外落进 localStorage——这是原插件默认全量持久化容易踩的坑，这里直接
  // 设计成不可能踩到。
  store.$subscribe(() => {
    const snapshot: Record<string, unknown> = {}
    for (const key of pick) snapshot[key] = (store.$state as Record<string, unknown>)[key]
    localStorage.setItem(storageKey, JSON.stringify(snapshot))
  })
}
