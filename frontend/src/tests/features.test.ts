import { mount, flushPromises } from "@vue/test-utils";
import { beforeEach, describe, it, expect, vi } from "vitest";
import FeaturesPanel from "../components/FeaturesPanel.vue";
import ShareView from "../components/ShareView.vue";
import ComposerEditor from "../components/ComposerEditor.vue";
import { EditorView } from "@codemirror/view";
import { getCM, Vim } from "@replit/codemirror-vim";
import { api } from "../api";
vi.mock("../api", () => ({
  api: vi.fn(),
  apiBase: "/api",
  json: (method: string, body: any) => ({ method, body: JSON.stringify(body) }),
}));
const initial = () => ({
  preferences: {
    vim: false,
    voice: false,
    advisor: false,
    notifications: true,
    keybindings: {
      send: "Enter",
      new: "Alt+n",
      stop: "Escape",
      focus: "Alt+i",
    },
  },
  shares: [
    {
      id: "share123",
      title: "test share",
      revoked: false,
      expires_at: 2000000000,
    },
  ],
  aliases: {},
  macros: [],
  feedback: [],
  stickers: [],
  notifications: [],
  privacy: { store_history: true, store_traces: true, allow_cloud: true },
  rate_limit: { rpm: 120, concurrency: 4 },
  speech: { ASR: true, TTS: true },
});
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api).mockImplementation(async (path: string) =>
    path === "/features"
      ? initial()
      : path === "/sessions"
        ? [{ id: "s1", title: "Test session" }]
        : { kind: "result", result: { ok: true } },
  );
});
async function click(wrapper: any, text: string) {
  await wrapper
    .findAll("button")
    .find((b: any) => b.text() === text)
    .trigger("click");
  await flushPromises();
}
describe("feature workbench", () => {
  it("saves real preferences through the API", async () => {
    const w = mount(FeaturesPanel);
    await flushPromises();
    await w.find("input[type=checkbox]").setValue(true);
    await flushPromises();
    expect(api).toHaveBeenCalledWith(
      "/features/preferences",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({ vim: true }),
      }),
    );
    w.unmount();
  });
  it("saves privacy options and rate limits as commands", async () => {
    const w = mount(FeaturesPanel);
    await flushPromises();
    await click(w, "隐私与限流");
    await w.find("input[type=checkbox]").setValue(false);
    await click(w, "保存隐私设置");
    const call = vi.mocked(api).mock.calls.find((c) => c[0] === "/commands")!;
    expect(JSON.parse(String(call[1]?.body)).text).toContain(
      '"store_history":false',
    );
    await click(w, "保存限流设置");
    expect(
      vi
        .mocked(api)
        .mock.calls.some((c) =>
          String(c[1]?.body).includes("/rate-limit 120 4"),
        ),
    ).toBe(true);
    w.unmount();
  });
  it("revokes snapshots and includes the selected permission", async () => {
    const w = mount(FeaturesPanel);
    await flushPromises();
    await click(w, "分享与反馈");
    await click(w, "撤销");
    const call = vi.mocked(api).mock.calls.find((c) => c[0] === "/commands")!;
    expect(JSON.parse(String(call[1]?.body))).toEqual({
      text: "/share revoke share123",
      session_id: null,
      permission: "workspace-write",
    });
    w.unmount();
  });
  it("shows backend failures without a success result", async () => {
    const w = mount(FeaturesPanel);
    await flushPromises();
    vi.mocked(api).mockRejectedValueOnce(Error("migration checksum mismatch"));
    await w.find("form").trigger("submit");
    await flushPromises();
    expect(w.find("[role=alert]").text()).toContain(
      "migration checksum mismatch",
    );
    w.unmount();
  });
  it("records and replays named macros through real command requests", async () => {
    const w = mount(FeaturesPanel);
    await flushPromises();
    await click(w, "宏与别名");
    await click(w, "开始录制");
    await click(w, "结束录制");
    const texts = vi
      .mocked(api)
      .mock.calls.filter((c) => c[0] === "/commands")
      .map((c) => JSON.parse(String(c[1]?.body)).text);
    expect(texts).toEqual(["/macro record 学习工作流", "/macro stop"]);
    w.unmount();
  });
  it("renders a read-only snapshot and expired-link errors", async () => {
    vi.mocked(api).mockResolvedValueOnce({
      title: "Shared",
      messages: [
        { role: "user", content: "hello" },
        { role: "assistant", content: "<script>bad()</script>answer" },
      ],
      expires_at: 2000000000,
    });
    let w = mount(ShareView, { props: { token: "abc" } });
    await flushPromises();
    expect(w.text()).toContain("Shared");
    expect(w.find("script").exists()).toBe(false);
    expect(w.find("textarea").exists()).toBe(false);
    w.unmount();
    vi.mocked(api).mockRejectedValueOnce(Error("分享已过期"));
    w = mount(ShareView, { props: { token: "expired" } });
    await flushPromises();
    expect(w.find("[role=alert]").text()).toContain("分享已过期");
    w.unmount();
  });
});
describe("Vim composer", () => {
  it("executes actual Vim normal-mode deletion and emits text", async () => {
    const w = mount(ComposerEditor, {
      attachTo: document.body,
      props: {
        modelValue: "alpha\nbeta",
        vim: true,
        disabled: false,
        keys: {
          send: "Ctrl+Enter",
          new: "Alt+n",
          stop: "Escape",
          focus: "Alt+i",
        },
      },
    });
    await flushPromises();
    const view = EditorView.findFromDOM(
      w.find(".cm-editor").element as HTMLElement,
    )!;
    const cm = getCM(view)!;
    expect(cm).toBeTruthy();
    Vim.handleKey(cm, "d", "test");
    Vim.handleKey(cm, "d", "test");
    expect(view.state.doc.toString()).toBe("beta");
    expect(w.emitted("update:modelValue")?.at(-1)).toEqual(["beta"]);
    await w.setProps({ disabled: true });
    expect(w.find(".cm-content").attributes("contenteditable")).toBe("false");
    w.unmount();
  });
  it("uses configured send shortcut and synchronizes external drafts", async () => {
    const w = mount(ComposerEditor, {
      attachTo: document.body,
      props: {
        modelValue: "draft",
        vim: false,
        disabled: false,
        keys: {
          send: "Ctrl+Enter",
          new: "Alt+n",
          stop: "Escape",
          focus: "Alt+i",
        },
      },
    });
    await flushPromises();
    await w
      .find(".cm-content")
      .trigger("keydown", { key: "Enter", ctrlKey: true });
    expect(w.emitted("send")).toHaveLength(1);
    await w.setProps({ modelValue: "new draft" });
    expect(w.find(".cm-content").text()).toContain("new draft");
    w.unmount();
  });
});
