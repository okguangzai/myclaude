<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, watch } from "vue";
import { EditorState, Compartment } from "@codemirror/state";
import { EditorView, keymap, placeholder } from "@codemirror/view";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { vim as vimMode } from "@replit/codemirror-vim";
const props = defineProps<{
  modelValue: string;
  vim: boolean;
  disabled: boolean;
  keys: Record<string, string>;
}>();
const emit = defineEmits(["update:modelValue", "send", "new", "stop"]);
const host = ref<HTMLElement>();
let view: EditorView;
const mode = new Compartment(),
  editable = new Compartment(),
  shortcuts = new Compartment();
function bindings() {
  return keymap.of(
    Object.entries(props.keys).map(([action, key]) => ({
      key: key.replaceAll("+", "-"),
      run: () => {
        if (action === "focus") view.focus();
        else emit(action as "send" | "new" | "stop");
        return true;
      },
    })),
  );
}
onMounted(() => {
  view = new EditorView({
    parent: host.value,
    state: EditorState.create({
      doc: props.modelValue,
      extensions: [
        mode.of(props.vim ? vimMode() : []),
        shortcuts.of(bindings()),
        history(),
        keymap.of([...defaultKeymap, ...historyKeymap]),
        EditorView.lineWrapping,
        placeholder("描述你的问题或任务，或输入 /help 查看命令…"),
        editable.of(EditorView.editable.of(!props.disabled)),
        EditorView.contentAttributes.of({ "aria-label": "输入消息" }),
        EditorView.updateListener.of((update) => {
          if (update.docChanged)
            emit("update:modelValue", update.state.doc.toString());
        }),
      ],
    }),
  });
});
watch(
  () => props.modelValue,
  (value) => {
    if (view && view.state.doc.toString() !== value)
      view.dispatch({
        changes: { from: 0, to: view.state.doc.length, insert: value },
      });
  },
);
watch(
  () => props.vim,
  (value) =>
    view?.dispatch({ effects: mode.reconfigure(value ? vimMode() : []) }),
);
watch(
  () => props.disabled,
  (value) =>
    view?.dispatch({
      effects: editable.reconfigure(EditorView.editable.of(!value)),
    }),
);
watch(
  () => props.keys,
  () => view?.dispatch({ effects: shortcuts.reconfigure(bindings()) }),
  { deep: true },
);
onBeforeUnmount(() => view?.destroy());
defineExpose({ focus: () => view?.focus() });
</script>
<template>
  <div ref="host" class="composer-editor" :class="{ 'vim-mode': vim }"></div>
  <small v-if="vim">Vim：Esc 普通模式 · i 插入模式</small>
</template>
<style>
.composer-editor .cm-content {
  min-height: 90px;
  padding: 12px;
  font-family: inherit;
}
.composer-editor .cm-focused {
  outline: none;
}
.composer-editor .cm-scroller {
  max-height: 260px;
}
.vim-mode {
  border-left: 3px solid #b87f48;
}
</style>
