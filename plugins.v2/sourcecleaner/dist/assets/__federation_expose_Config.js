import { importShared } from './__federation_fn_import.js';
import { _ as _export_sfc } from './_plugin-vue_export-helper.js';

const _sfc_main = {
  name: 'Config',
  props: {
    initialConfig: { type: Object, default: () => ({}) },
    api: { type: Object, required: true },
    pluginId: { type: String, required: true },
  },
  emits: ['save', 'close'],
  data() {
    return { config: { ...this.initialConfig } };
  },
  methods: {
    async save() {
      try {
        await this.api.post(`plugin/${this.pluginId}/config`, this.config);
        this.$emit('save', this.config);
      } catch (e) {
        alert('保存失败');
      }
    },
  },
};

const {createTextVNode:_createTextVNode,resolveComponent:_resolveComponent,withCtx:_withCtx,createVNode:_createVNode,openBlock:_openBlock,createBlock:_createBlock} = await importShared('vue');


function _sfc_render(_ctx, _cache, $props, $setup, $data, $options) {
  const _component_v_card_title = _resolveComponent("v-card-title");
  const _component_v_switch = _resolveComponent("v-switch");
  const _component_v_textarea = _resolveComponent("v-textarea");
  const _component_v_select = _resolveComponent("v-select");
  const _component_v_card_text = _resolveComponent("v-card-text");
  const _component_v_spacer = _resolveComponent("v-spacer");
  const _component_v_btn = _resolveComponent("v-btn");
  const _component_v_card_actions = _resolveComponent("v-card-actions");
  const _component_v_card = _resolveComponent("v-card");

  return (_openBlock(), _createBlock(_component_v_card, { variant: "flat" }, {
    default: _withCtx(() => [
      _createVNode(_component_v_card_title, null, {
        default: _withCtx(() => [...(_cache[4] || (_cache[4] = [
          _createTextVNode("源文件清理设置", -1)
        ]))]),
        _: 1
      }),
      _createVNode(_component_v_card_text, null, {
        default: _withCtx(() => [
          _createVNode(_component_v_switch, {
            modelValue: $data.config.enabled,
            "onUpdate:modelValue": _cache[0] || (_cache[0] = $event => (($data.config.enabled) = $event)),
            label: "启用插件",
            color: "primary"
          }, null, 8, ["modelValue"]),
          _createVNode(_component_v_textarea, {
            modelValue: $data.config.scan_dirs,
            "onUpdate:modelValue": _cache[1] || (_cache[1] = $event => (($data.config.scan_dirs) = $event)),
            label: "扫描目录（每行一个）",
            rows: "2",
            variant: "outlined",
            density: "compact"
          }, null, 8, ["modelValue"]),
          _createVNode(_component_v_select, {
            modelValue: $data.config.scan_scope,
            "onUpdate:modelValue": _cache[2] || (_cache[2] = $event => (($data.config.scan_scope) = $event)),
            items: [{title:'仅媒体库',value:'media_only'},{title:'仅源数据',value:'source_only'},{title:'全部',value:'all'}],
            label: "扫描范围",
            variant: "outlined",
            density: "compact"
          }, null, 8, ["modelValue"]),
          _createVNode(_component_v_switch, {
            modelValue: $data.config.allow_delete,
            "onUpdate:modelValue": _cache[3] || (_cache[3] = $event => (($data.config.allow_delete) = $event)),
            label: "允许删除",
            color: "error"
          }, null, 8, ["modelValue"])
        ]),
        _: 1
      }),
      _createVNode(_component_v_card_actions, null, {
        default: _withCtx(() => [
          _createVNode(_component_v_spacer),
          _createVNode(_component_v_btn, {
            color: "primary",
            onClick: $options.save
          }, {
            default: _withCtx(() => [...(_cache[5] || (_cache[5] = [
              _createTextVNode("保存", -1)
            ]))]),
            _: 1
          }, 8, ["onClick"])
        ]),
        _: 1
      })
    ]),
    _: 1
  }))
}
const Config = /*#__PURE__*/_export_sfc(_sfc_main, [['render',_sfc_render]]);

export { Config as default };
