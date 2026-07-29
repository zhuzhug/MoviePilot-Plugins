import { importShared } from './__federation_fn_import.js';
import { _ as _export_sfc } from './_plugin-vue_export-helper.js';

const _sfc_main = {
  name: 'AppPage',
  props: {
    api: { type: Object, required: true },
    pluginId: { type: String, required: true },
    navKey: { type: String, default: 'main' },
  },
  data: () => ({
    scanning: false,
    summary: null,
    items: {},
    expandedPanels: [],
    selectedTab: null,
    maxDisplay: 200,
    allowDelete: false,
    categories: [
      { id: 'dangling', title: '悬空软链', icon: 'mdi-link-variant-off', safety: 'safe', safetyLabel: '安全可删', safetyDesc: '软链目标已不存在，删除链接无任何影响' },
      { id: 'orphan_meta', title: '孤儿元数据', icon: 'mdi-file-document-outline', safety: 'safe', safetyLabel: '安全可删', safetyDesc: '无对应视频的 .nfo/.jpg/.srt，不影响播放' },
      { id: 'empty_dir', title: '空目录', icon: 'mdi-folder-open-outline', safety: 'safe', safetyLabel: '安全可删', safetyDesc: '空目录可直接删除' },
      { id: 'dup_resource', title: '重复资源', icon: 'mdi-content-duplicate', safety: 'warn', safetyLabel: '需确认', safetyDesc: '同片不同版本，删除前确认保留哪个' },
      { id: 'source_transferred', title: '已入库源文件', icon: 'mdi-file-check', safety: 'danger', safetyLabel: '谨慎', safetyDesc: '下载目录中已整理到媒体库的文件' },
      { id: 'source_orphan', title: '孤立源文件', icon: 'mdi-file-question', safety: 'danger', safetyLabel: '谨慎', safetyDesc: '无下载任务跟踪，可能被其他工具使用' },
      { id: 'source_torrent', title: '无效种子文件', icon: 'mdi-file-remove-outline', safety: 'warn', safetyLabel: '需确认', safetyDesc: '.torrent 文件残留' },
      { id: 'source_empty_dir', title: '源目录空目录', icon: 'mdi-folder-open-outline', safety: 'safe', safetyLabel: '安全可删', safetyDesc: '源数据空目录' },
      { id: 'source_dup', title: '源文件重复', icon: 'mdi-file-multiple', safety: 'warn', safetyLabel: '需确认', safetyDesc: '下载目录中的重复文件' },
    ],
  }),
  mounted() {
    this.fetchResult();
  },
  methods: {
    async fetchResult() {
      try {
        const resp = await this.api.get(`plugin/${this.pluginId}/result`);
        if (resp.data) {
          this.summary = resp.data;
          this.items = resp.data.items || {};
          this.maxDisplay = resp.data.max_display || 200;
          this.allowDelete = resp.data.allow_delete || false;
        }
      } catch (e) {
        console.error('获取结果失败', e);
      }
    },
    async startScan() {
      this.scanning = true;
      try {
        await this.api.get(`plugin/${this.pluginId}/scan`);
        await this.fetchResult();
      } catch (e) {
        console.error('扫描失败', e);
      } finally {
        this.scanning = false;
      }
    },
    async cancelScan() {
      try {
        await this.api.get(`plugin/${this.pluginId}/cancel`);
      } catch (e) {
        console.error('取消失败', e);
      }
    },
    async deleteItem(path, category) {
      if (!confirm(`确认删除？\n${path}`)) return;
      try {
        await this.api.post(`plugin/${this.pluginId}/delete_item`, { path, category });
        await this.fetchResult();
      } catch (e) {
        alert('删除失败：' + (e.message || e));
      }
    },
    getItems(categoryId) {
      return (this.items[categoryId] || []).slice(0, this.maxDisplay);
    },
    getCategorySize(categoryId) {
      return (this.items[categoryId] || []).reduce((sum, it) => sum + (it.size || 0), 0);
    },
    formatSize(n) {
      if (!n || n === 0) return '0 B';
      const units = ['B', 'KB', 'MB', 'GB', 'TB'];
      let i = 0;
      let size = n;
      while (Math.abs(size) >= 1024 && i < units.length - 1) { size /= 1024; i++; }
      return size.toFixed(1) + ' ' + units[i];
    },
    getSafetyColor(safety) {
      return { safe: 'success', warn: 'warning', danger: 'error' }[safety] || 'grey';
    },
    getSafetyType(safety) {
      return { safe: 'success', warn: 'warning', danger: 'error' }[safety] || 'info';
    },
  },
};

const {createTextVNode:_createTextVNode,resolveComponent:_resolveComponent,withCtx:_withCtx,createVNode:_createVNode,openBlock:_openBlock,createBlock:_createBlock,createCommentVNode:_createCommentVNode,toDisplayString:_toDisplayString,createElementVNode:_createElementVNode,renderList:_renderList,Fragment:_Fragment,createElementBlock:_createElementBlock,createSlots:_createSlots} = await importShared('vue');


const _hoisted_1 = { class: "text-body-2 font-weight-bold" };
const _hoisted_2 = { key: 0 };
const _hoisted_3 = { key: 1 };

function _sfc_render(_ctx, _cache, $props, $setup, $data, $options) {
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
          (_ctx.scanning)
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
                    width: "2",
                    class: "mr-1"
                  }),
                  _cache[2] || (_cache[2] = _createTextVNode(" 扫描中... ", -1))
                ]),
                _: 1
              }))
            : (_ctx.summary)
              ? (_openBlock(), _createBlock(_component_v_chip, {
                  key: 1,
                  color: "primary",
                  variant: "tonal",
                  size: "small",
                  class: "mr-2"
                }, {
                  default: _withCtx(() => [
                    _createTextVNode(" 共 " + _toDisplayString(_ctx.summary.total) + " 项 · " + _toDisplayString($options.formatSize(_ctx.summary.total_size)), 1)
                  ]),
                  _: 1
                }))
              : _createCommentVNode("", true),
          (_ctx.scanning)
            ? (_openBlock(), _createBlock(_component_v_btn, {
                key: 2,
                color: "error",
                variant: "tonal",
                size: "small",
                onClick: $options.cancelScan
              }, {
                default: _withCtx(() => [
                  _createVNode(_component_v_icon, { start: "" }, {
                    default: _withCtx(() => [...(_cache[3] || (_cache[3] = [
                      _createTextVNode("mdi-close-circle", -1)
                    ]))]),
                    _: 1
                  }),
                  _cache[4] || (_cache[4] = _createTextVNode("取消 ", -1))
                ]),
                _: 1
              }, 8, ["onClick"]))
            : (_openBlock(), _createBlock(_component_v_btn, {
                key: 3,
                color: "primary",
                variant: "flat",
                size: "small",
                onClick: $options.startScan,
                loading: _ctx.scanning
              }, {
                default: _withCtx(() => [
                  _createVNode(_component_v_icon, { start: "" }, {
                    default: _withCtx(() => [...(_cache[5] || (_cache[5] = [
                      _createTextVNode("mdi-refresh", -1)
                    ]))]),
                    _: 1
                  }),
                  _cache[6] || (_cache[6] = _createTextVNode("扫描 ", -1))
                ]),
                _: 1
              }, 8, ["onClick", "loading"]))
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
              (_ctx.scanning)
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
                          _cache[8] || (_cache[8] = _createElementVNode("div", { class: "text-caption text-medium-emphasis" }, "扫描过程中请勿关闭页面", -1)),
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
                : (_ctx.summary && _ctx.summary.total > 0)
                  ? (_openBlock(), _createElementBlock(_Fragment, { key: 1 }, [
                      _createVNode(_component_v_row, { class: "mb-4" }, {
                        default: _withCtx(() => [
                          (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(_ctx.categories, (cat) => {
                            return (_openBlock(), _createBlock(_component_v_col, {
                              key: cat.id,
                              cols: "6",
                              md: "3"
                            }, {
                              default: _withCtx(() => [
                                _createVNode(_component_v_card, {
                                  variant: "outlined",
                                  color: $options.getSafetyColor(cat.safety),
                                  class: "cursor-pointer",
                                  onClick: $event => (_ctx.selectedTab = cat.id)
                                }, {
                                  default: _withCtx(() => [
                                    _createVNode(_component_v_card_text, { class: "text-center py-3" }, {
                                      default: _withCtx(() => [
                                        _createVNode(_component_v_icon, {
                                          color: $options.getSafetyColor(cat.safety),
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
                                          color: $options.getSafetyColor(cat.safety),
                                          variant: "flat",
                                          size: "small",
                                          class: "mt-1"
                                        }, {
                                          default: _withCtx(() => [
                                            _createTextVNode(_toDisplayString(_ctx.summary.counts[cat.id] || 0), 1)
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
                            }, 1024))
                          }), 128))
                        ]),
                        _: 1
                      }),
                      _createVNode(_component_v_expansion_panels, {
                        modelValue: _ctx.expandedPanels,
                        "onUpdate:modelValue": _cache[0] || (_cache[0] = $event => ((_ctx.expandedPanels) = $event))
                      }, {
                        default: _withCtx(() => [
                          (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(_ctx.categories, (cat) => {
                            return (_openBlock(), _createBlock(_component_v_expansion_panel, {
                              key: cat.id,
                              value: cat.id
                            }, {
                              default: _withCtx(() => [
                                _createVNode(_component_v_expansion_panel_title, null, {
                                  default: _withCtx(() => [
                                    _createVNode(_component_v_icon, {
                                      color: $options.getSafetyColor(cat.safety),
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
                                      color: $options.getSafetyColor(cat.safety),
                                      variant: "tonal",
                                      size: "x-small",
                                      class: "mr-2",
                                      "prepend-icon": "mdi-shield"
                                    }, {
                                      default: _withCtx(() => [
                                        _createTextVNode(_toDisplayString(cat.safetyLabel), 1)
                                      ]),
                                      _: 2
                                    }, 1032, ["color"]),
                                    _createVNode(_component_v_chip, {
                                      color: "warning",
                                      variant: "flat",
                                      size: "small"
                                    }, {
                                      default: _withCtx(() => [
                                        _createTextVNode(_toDisplayString(_ctx.summary.counts[cat.id] || 0) + " · " + _toDisplayString($options.formatSize($options.getCategorySize(cat.id))), 1)
                                      ]),
                                      _: 2
                                    }, 1024)
                                  ]),
                                  _: 2
                                }, 1024),
                                _createVNode(_component_v_expansion_panel_text, null, {
                                  default: _withCtx(() => [
                                    _createVNode(_component_v_alert, {
                                      type: $options.getSafetyType(cat.safety),
                                      variant: "tonal",
                                      density: "compact",
                                      class: "mb-3"
                                    }, {
                                      default: _withCtx(() => [
                                        _createElementVNode("strong", null, _toDisplayString(cat.safetyLabel), 1),
                                        _createTextVNode("：" + _toDisplayString(cat.safetyDesc), 1)
                                      ]),
                                      _: 2
                                    }, 1032, ["type"]),
                                    _createVNode(_component_v_list, {
                                      density: "compact",
                                      lines: "two"
                                    }, {
                                      default: _withCtx(() => [
                                        (_openBlock(true), _createElementBlock(_Fragment, null, _renderList($options.getItems(cat.id), (item, idx) => {
                                          return (_openBlock(), _createBlock(_component_v_list_item, { key: idx }, _createSlots({
                                            default: _withCtx(() => [
                                              _createVNode(_component_v_list_item_title, { style: {"word-break":"break-all","font-family":"monospace","font-size":"0.875rem"} }, {
                                                default: _withCtx(() => [
                                                  _createTextVNode(_toDisplayString(item.path), 1)
                                                ]),
                                                _: 2
                                              }, 1024),
                                              _createVNode(_component_v_list_item_subtitle, null, {
                                                default: _withCtx(() => [
                                                  (item.size)
                                                    ? (_openBlock(), _createElementBlock("span", _hoisted_2, "大小: " + _toDisplayString($options.formatSize(item.size)), 1))
                                                    : _createCommentVNode("", true),
                                                  (item.target)
                                                    ? (_openBlock(), _createElementBlock("span", _hoisted_3, " · " + _toDisplayString(item.target), 1))
                                                    : _createCommentVNode("", true)
                                                ]),
                                                _: 2
                                              }, 1024)
                                            ]),
                                            _: 2
                                          }, [
                                            (_ctx.allowDelete)
                                              ? {
                                                  name: "append",
                                                  fn: _withCtx(() => [
                                                    _createVNode(_component_v_btn, {
                                                      icon: "mdi-delete",
                                                      color: "error",
                                                      variant: "text",
                                                      size: "small",
                                                      onClick: $event => ($options.deleteItem(item.path, cat.id))
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
                                    (_ctx.summary.truncated[cat.id])
                                      ? (_openBlock(), _createBlock(_component_v_alert, {
                                          key: 0,
                                          type: "info",
                                          variant: "tonal",
                                          density: "compact",
                                          class: "mt-2"
                                        }, {
                                          default: _withCtx(() => [
                                            _createTextVNode(" 已达到展示上限（" + _toDisplayString(_ctx.maxDisplay) + "），如需查看更多请调整设置 ", 1)
                                          ]),
                                          _: 1
                                        }))
                                      : _createCommentVNode("", true)
                                  ]),
                                  _: 2
                                }, 1024)
                              ]),
                              _: 2
                            }, 1032, ["value"]))
                          }), 128))
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
                              default: _withCtx(() => [...(_cache[9] || (_cache[9] = [
                                _createTextVNode("mdi-magnify-close", -1)
                              ]))]),
                              _: 1
                            }),
                            _cache[10] || (_cache[10] = _createElementVNode("div", { class: "text-h6 text-grey" }, "暂无扫描结果", -1)),
                            _cache[11] || (_cache[11] = _createElementVNode("div", { class: "text-caption text-medium-emphasis mt-2" }, "点击右上角\"扫描\"按钮开始检测", -1))
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
const AppPage = /*#__PURE__*/_export_sfc(_sfc_main, [['render',_sfc_render]]);

export { AppPage as default };
