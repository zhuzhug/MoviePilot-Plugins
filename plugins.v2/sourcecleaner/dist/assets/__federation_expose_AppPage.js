import { importShared } from './__federation_fn_import.js';

const {createTextVNode:_createTextVNode,resolveComponent:_resolveComponent,withCtx:_withCtx,createVNode:_createVNode,openBlock:_openBlock,createBlock:_createBlock,createCommentVNode:_createCommentVNode,toDisplayString:_toDisplayString,createElementVNode:_createElementVNode,renderList:_renderList,Fragment:_Fragment,createElementBlock:_createElementBlock,createSlots:_createSlots} = await importShared('vue');


const _hoisted_1 = { class: "text-body-2 font-weight-bold" };
const _hoisted_2 = { key: 0 };
const _hoisted_3 = { key: 1 };

const {ref,onMounted} = await importShared('vue');



const _sfc_main = {
  __name: 'AppPage',
  props: {
  api: { type: Object, required: true },
  pluginId: { type: String, required: true },
},
  setup(__props) {

const props = __props;

const scanning = ref(false);
const summary = ref(null);
const items = ref({});
const expanded = ref([]);
const allowDelete = ref(false);
const maxDisplay = ref(200);

const categories = [
  { id: 'dangling', title: '悬空软链', icon: 'mdi-link-variant-off', s: 'success', sl: '安全可删', sd: '目标已不存在' },
  { id: 'orphan_meta', title: '孤儿元数据', icon: 'mdi-file-document-outline', s: 'success', sl: '安全可删', sd: '无对应视频' },
  { id: 'empty_dir', title: '空目录', icon: 'mdi-folder-open-outline', s: 'success', sl: '安全可删', sd: '空目录' },
  { id: 'dup_resource', title: '重复资源', icon: 'mdi-content-duplicate', s: 'warning', sl: '需确认', sd: '同片不同版本' },
  { id: 'source_transferred', title: '已入库源文件', icon: 'mdi-file-check', s: 'error', sl: '谨慎', sd: '已整理到库' },
  { id: 'source_orphan', title: '孤立源文件', icon: 'mdi-file-question', s: 'error', sl: '谨慎', sd: '无下载器跟踪' },
  { id: 'source_torrent', title: '无效种子文件', icon: 'mdi-file-remove-outline', s: 'warning', sl: '需确认', sd: '.torrent 残留' },
  { id: 'source_empty_dir', title: '源目录空目录', icon: 'mdi-folder-open-outline', s: 'success', sl: '安全可删', sd: '源数据空目录' },
  { id: 'source_dup', title: '源文件重复', icon: 'mdi-file-multiple', s: 'warning', sl: '需确认', sd: '下载目录重复' },
];

function unwrap(r) {
  if (r && r.data !== undefined && (r.success !== undefined || r.code !== undefined)) return r.data
  return r?.data ?? r
}

onMounted(() => fetchResult());

async function fetchResult() {
  try {
    const resp = await props.api.get(`plugin/${props.pluginId}/result`);
    const d = unwrap(resp);
    if (d) {
      summary.value = d;
      items.value = d.items || {};
      maxDisplay.value = d.max_display || 200;
      allowDelete.value = d.allow_delete || false;
    }
  } catch (e) { console.error('fetchResult error:', e); }
}

async function startScan() {
  scanning.value = true;
  try {
    const resp = await props.api.get(`plugin/${props.pluginId}/scan`);
    const d = unwrap(resp);
    if (d) {
      summary.value = d;
      items.value = d.items || {};
    }
  } catch (e) { console.error('scan error:', e); }
  finally { scanning.value = false; }
}

async function cancelScan() {
  try { await props.api.get(`plugin/${props.pluginId}/cancel`); } catch (e) {}
}

async function delItem(path, category) {
  if (!confirm(`确认删除？\n${path}`)) return
  try {
    await props.api.post(`plugin/${props.pluginId}/delete_item`, { path, category });
    await fetchResult();
  } catch (e) { alert('删除失败'); }
}

function catItems(id) { return (items.value[id] || []).slice(0, maxDisplay.value) }
function catSize(id) { return (items.value[id] || []).reduce((s, it) => s + (it.size || 0), 0) }
function fmtSize(n) {
  if (!n) return '0 B'
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, s = n;
  while (Math.abs(s) >= 1024 && i < u.length - 1) { s /= 1024; i++; }
  return s.toFixed(1) + ' ' + u[i]
}
function sColor(s) { return { success: 'success', warning: 'warning', error: 'error' }[s] || 'grey' }

return (_ctx, _cache) => {
  const _component_v_app_bar_title = _resolveComponent("v-app-bar-title");
  const _component_v_progress_circular = _resolveComponent("v-progress-circular");
  const _component_v_chip = _resolveComponent("v-chip");
  const _component_v_icon = _resolveComponent("v-icon");
  const _component_v_btn = _resolveComponent("v-btn");
  const _component_v_app_bar = _resolveComponent("v-app-bar");
  const _component_v_progress_linear = _resolveComponent("v-progress-linear");
  const _component_v_card_text = _resolveComponent("v-card-text");
  const _component_v_card = _resolveComponent("v-card");
  const _component_v_col = _resolveComponent("v-col");
  const _component_v_row = _resolveComponent("v-row");
  const _component_v_spacer = _resolveComponent("v-spacer");
  const _component_v_expansion_panel_title = _resolveComponent("v-expansion-panel-title");
  const _component_v_alert = _resolveComponent("v-alert");
  const _component_v_list_item_title = _resolveComponent("v-list-item-title");
  const _component_v_list_item_subtitle = _resolveComponent("v-list-item-subtitle");
  const _component_v_list_item = _resolveComponent("v-list-item");
  const _component_v_list = _resolveComponent("v-list");
  const _component_v_expansion_panel_text = _resolveComponent("v-expansion-panel-text");
  const _component_v_expansion_panel = _resolveComponent("v-expansion-panel");
  const _component_v_expansion_panels = _resolveComponent("v-expansion-panels");
  const _component_v_container = _resolveComponent("v-container");
  const _component_v_main = _resolveComponent("v-main");
  const _component_v_app = _resolveComponent("v-app");

  return (_openBlock(), _createBlock(_component_v_app, null, {
    default: _withCtx(() => [
      _createVNode(_component_v_app_bar, {
        flat: "",
        density: "compact"
      }, {
        append: _withCtx(() => [
          (scanning.value)
            ? (_openBlock(), _createBlock(_component_v_chip, {
                key: 0,
                color: "info",
                variant: "tonal",
                size: "small",
                class: "mr-2"
              }, {
                default: _withCtx(() => [
                  _createVNode(_component_v_progress_circular, {
                    indeterminate: "",
                    size: "16",
                    width: "1",
                    class: "mr-1"
                  }),
                  _cache[2] || (_cache[2] = _createTextVNode(" 扫描中... ", -1))
                ]),
                _: 1
              }))
            : (summary.value)
              ? (_openBlock(), _createBlock(_component_v_chip, {
                  key: 1,
                  color: "primary",
                  variant: "tonal",
                  size: "small",
                  class: "mr-2"
                }, {
                  default: _withCtx(() => [
                    _createTextVNode(" 共 " + _toDisplayString(summary.value.total) + " 项 · " + _toDisplayString(fmtSize(summary.value.total_size)), 1)
                  ]),
                  _: 1
                }))
              : _createCommentVNode("", true),
          (scanning.value)
            ? (_openBlock(), _createBlock(_component_v_btn, {
                key: 2,
                color: "error",
                variant: "tonal",
                size: "small",
                onClick: cancelScan
              }, {
                default: _withCtx(() => [
                  _createVNode(_component_v_icon, {
                    start: "",
                    size: "small"
                  }, {
                    default: _withCtx(() => [...(_cache[3] || (_cache[3] = [
                      _createTextVNode("mdi-close-circle", -1)
                    ]))]),
                    _: 1
                  }),
                  _cache[4] || (_cache[4] = _createTextVNode("取消 ", -1))
                ]),
                _: 1
              }))
            : (_openBlock(), _createBlock(_component_v_btn, {
                key: 3,
                color: "primary",
                variant: "flat",
                size: "small",
                onClick: startScan
              }, {
                default: _withCtx(() => [
                  _createVNode(_component_v_icon, {
                    start: "",
                    size: "small"
                  }, {
                    default: _withCtx(() => [...(_cache[5] || (_cache[5] = [
                      _createTextVNode("mdi-refresh", -1)
                    ]))]),
                    _: 1
                  }),
                  _cache[6] || (_cache[6] = _createTextVNode("扫描 ", -1))
                ]),
                _: 1
              }))
        ]),
        default: _withCtx(() => [
          _createVNode(_component_v_app_bar_title, null, {
            default: _withCtx(() => [...(_cache[1] || (_cache[1] = [
              _createTextVNode("源文件清理", -1)
            ]))]),
            _: 1
          })
        ]),
        _: 1
      }),
      _createVNode(_component_v_main, null, {
        default: _withCtx(() => [
          _createVNode(_component_v_container, { fluid: "" }, {
            default: _withCtx(() => [
              (scanning.value)
                ? (_openBlock(), _createBlock(_component_v_card, {
                    key: 0,
                    variant: "outlined",
                    class: "mb-4"
                  }, {
                    default: _withCtx(() => [
                      _createVNode(_component_v_card_text, { class: "text-center py-8" }, {
                        default: _withCtx(() => [
                          _createVNode(_component_v_progress_circular, {
                            indeterminate: "",
                            color: "primary",
                            size: "64",
                            class: "mb-4"
                          }),
                          _cache[7] || (_cache[7] = _createElementVNode("div", { class: "text-h6 mb-2" }, "正在扫描文件系统...", -1)),
                          _cache[8] || (_cache[8] = _createElementVNode("div", { class: "text-caption text-medium-emphasis" }, "请稍候", -1)),
                          _createVNode(_component_v_progress_linear, {
                            indeterminate: "",
                            color: "primary",
                            class: "mt-4"
                          })
                        ]),
                        _: 1
                      })
                    ]),
                    _: 1
                  }))
                : (summary.value && summary.value.total > 0)
                  ? (_openBlock(), _createElementBlock(_Fragment, { key: 1 }, [
                      _createVNode(_component_v_row, { class: "mb-4" }, {
                        default: _withCtx(() => [
                          (_openBlock(), _createElementBlock(_Fragment, null, _renderList(categories, (cat) => {
                            return _createVNode(_component_v_col, {
                              key: cat.id,
                              cols: "6",
                              md: "3"
                            }, {
                              default: _withCtx(() => [
                                _createVNode(_component_v_card, {
                                  variant: "outlined",
                                  color: sColor(cat.s),
                                  class: "cursor-pointer",
                                  onClick: $event => (expanded.value = [cat.id])
                                }, {
                                  default: _withCtx(() => [
                                    _createVNode(_component_v_card_text, { class: "text-center py-3" }, {
                                      default: _withCtx(() => [
                                        _createVNode(_component_v_icon, {
                                          color: sColor(cat.s),
                                          size: "28",
                                          class: "mb-1"
                                        }, {
                                          default: _withCtx(() => [
                                            _createTextVNode(_toDisplayString(cat.icon), 1)
                                          ]),
                                          _: 2
                                        }, 1032, ["color"]),
                                        _createElementVNode("div", _hoisted_1, _toDisplayString(cat.title), 1),
                                        _createVNode(_component_v_chip, {
                                          color: sColor(cat.s),
                                          variant: "flat",
                                          size: "small",
                                          class: "mt-1"
                                        }, {
                                          default: _withCtx(() => [
                                            _createTextVNode(_toDisplayString(summary.value.counts[cat.id] || 0), 1)
                                          ]),
                                          _: 2
                                        }, 1032, ["color"])
                                      ]),
                                      _: 2
                                    }, 1024)
                                  ]),
                                  _: 2
                                }, 1032, ["color", "onClick"])
                              ]),
                              _: 2
                            }, 1024)
                          }), 64))
                        ]),
                        _: 1
                      }),
                      _createVNode(_component_v_expansion_panels, {
                        modelValue: expanded.value,
                        "onUpdate:modelValue": _cache[0] || (_cache[0] = $event => ((expanded).value = $event))
                      }, {
                        default: _withCtx(() => [
                          (_openBlock(), _createElementBlock(_Fragment, null, _renderList(categories, (cat) => {
                            return _createVNode(_component_v_expansion_panel, {
                              key: cat.id,
                              value: cat.id
                            }, {
                              default: _withCtx(() => [
                                _createVNode(_component_v_expansion_panel_title, null, {
                                  default: _withCtx(() => [
                                    _createVNode(_component_v_icon, {
                                      color: sColor(cat.s),
                                      class: "mr-2"
                                    }, {
                                      default: _withCtx(() => [
                                        _createTextVNode(_toDisplayString(cat.icon), 1)
                                      ]),
                                      _: 2
                                    }, 1032, ["color"]),
                                    _createTextVNode(" " + _toDisplayString(cat.title) + " ", 1),
                                    _createVNode(_component_v_spacer),
                                    _createVNode(_component_v_chip, {
                                      color: sColor(cat.s),
                                      variant: "tonal",
                                      size: "x-small",
                                      class: "mr-2"
                                    }, {
                                      default: _withCtx(() => [
                                        _createTextVNode(_toDisplayString(cat.sl), 1)
                                      ]),
                                      _: 2
                                    }, 1032, ["color"]),
                                    _createVNode(_component_v_chip, {
                                      color: "warning",
                                      variant: "flat",
                                      size: "small"
                                    }, {
                                      default: _withCtx(() => [
                                        _createTextVNode(_toDisplayString(summary.value.counts[cat.id] || 0) + " · " + _toDisplayString(fmtSize(catSize(cat.id))), 1)
                                      ]),
                                      _: 2
                                    }, 1024)
                                  ]),
                                  _: 2
                                }, 1024),
                                _createVNode(_component_v_expansion_panel_text, null, {
                                  default: _withCtx(() => [
                                    _createVNode(_component_v_alert, {
                                      type: sColor(cat.s),
                                      variant: "tonal",
                                      density: "compact",
                                      class: "mb-3"
                                    }, {
                                      default: _withCtx(() => [
                                        _createElementVNode("strong", null, _toDisplayString(cat.sl), 1),
                                        _createTextVNode("：" + _toDisplayString(cat.sd), 1)
                                      ]),
                                      _: 2
                                    }, 1032, ["type"]),
                                    _createVNode(_component_v_list, {
                                      density: "compact",
                                      lines: "two"
                                    }, {
                                      default: _withCtx(() => [
                                        (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(catItems(cat.id), (it, i) => {
                                          return (_openBlock(), _createBlock(_component_v_list_item, { key: i }, _createSlots({
                                            default: _withCtx(() => [
                                              _createVNode(_component_v_list_item_title, { style: {"word-break":"break-all","font-family":"monospace","font-size":".875rem"} }, {
                                                default: _withCtx(() => [
                                                  _createTextVNode(_toDisplayString(it.path), 1)
                                                ]),
                                                _: 2
                                              }, 1024),
                                              _createVNode(_component_v_list_item_subtitle, null, {
                                                default: _withCtx(() => [
                                                  (it.size)
                                                    ? (_openBlock(), _createElementBlock("span", _hoisted_2, _toDisplayString(fmtSize(it.size)), 1))
                                                    : _createCommentVNode("", true),
                                                  (it.target)
                                                    ? (_openBlock(), _createElementBlock("span", _hoisted_3, " · " + _toDisplayString(it.target), 1))
                                                    : _createCommentVNode("", true)
                                                ]),
                                                _: 2
                                              }, 1024)
                                            ]),
                                            _: 2
                                          }, [
                                            (allowDelete.value)
                                              ? {
                                                  name: "append",
                                                  fn: _withCtx(() => [
                                                    _createVNode(_component_v_btn, {
                                                      icon: "mdi-delete",
                                                      color: "error",
                                                      variant: "text",
                                                      size: "small",
                                                      onClick: $event => (delItem(it.path, cat.id))
                                                    }, null, 8, ["onClick"])
                                                  ]),
                                                  key: "0"
                                                }
                                              : undefined
                                          ]), 1024))
                                        }), 128))
                                      ]),
                                      _: 2
                                    }, 1024),
                                    (summary.value.truncated[cat.id])
                                      ? (_openBlock(), _createBlock(_component_v_alert, {
                                          key: 0,
                                          type: "info",
                                          variant: "tonal",
                                          density: "compact",
                                          class: "mt-2"
                                        }, {
                                          default: _withCtx(() => [...(_cache[9] || (_cache[9] = [
                                            _createTextVNode("已达到展示上限", -1)
                                          ]))]),
                                          _: 1
                                        }))
                                      : _createCommentVNode("", true)
                                  ]),
                                  _: 2
                                }, 1024)
                              ]),
                              _: 2
                            }, 1032, ["value"])
                          }), 64))
                        ]),
                        _: 1
                      }, 8, ["modelValue"])
                    ], 64))
                  : (_openBlock(), _createBlock(_component_v_card, {
                      key: 2,
                      variant: "outlined"
                    }, {
                      default: _withCtx(() => [
                        _createVNode(_component_v_card_text, { class: "text-center py-12" }, {
                          default: _withCtx(() => [
                            _createVNode(_component_v_icon, {
                              size: "64",
                              color: "grey-lighten-1",
                              class: "mb-4"
                            }, {
                              default: _withCtx(() => [...(_cache[10] || (_cache[10] = [
                                _createTextVNode("mdi-magnify-close", -1)
                              ]))]),
                              _: 1
                            }),
                            _cache[11] || (_cache[11] = _createElementVNode("div", { class: "text-h6 text-grey" }, "暂无扫描结果", -1)),
                            _cache[12] || (_cache[12] = _createElementVNode("div", { class: "text-caption text-medium-emphasis mt-2" }, "点击右上角\"扫描\"按钮开始", -1))
                          ]),
                          _: 1
                        })
                      ]),
                      _: 1
                    }))
            ]),
            _: 1
          })
        ]),
        _: 1
      })
    ]),
    _: 1
  }))
}
}

};

export { _sfc_main as default };
