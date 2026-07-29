import { importShared } from './__federation_fn_import.js';

const {createTextVNode:_createTextVNode,resolveComponent:_resolveComponent,withCtx:_withCtx,createVNode:_createVNode,openBlock:_openBlock,createBlock:_createBlock} = await importShared('vue');


const {ref} = await importShared('vue');



const _sfc_main = {
  __name: 'Config',
  props: {
  initialConfig: { type: Object, default: () => ({}) },
},
  emits: ['save', 'close'],
  setup(__props, { emit: __emit }) {

const props = __props;

const emit = __emit;
const config = ref({ ...props.initialConfig });

return (_ctx, _cache) => {
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
        default: _withCtx(() => [...(_cache[5] || (_cache[5] = [
          _createTextVNode("源文件清理设置", -1)
        ]))]),
        _: 1
      }),
      _createVNode(_component_v_card_text, null, {
        default: _withCtx(() => [
          _createVNode(_component_v_switch, {
            modelValue: config.value.enabled,
            "onUpdate:modelValue": _cache[0] || (_cache[0] = $event => ((config.value.enabled) = $event)),
            label: "启用插件",
            color: "primary"
          }, null, 8, ["modelValue"]),
          _createVNode(_component_v_textarea, {
            modelValue: config.value.scan_dirs,
            "onUpdate:modelValue": _cache[1] || (_cache[1] = $event => ((config.value.scan_dirs) = $event)),
            label: "扫描目录（每行一个）",
            rows: "2",
            variant: "outlined",
            density: "compact"
          }, null, 8, ["modelValue"]),
          _createVNode(_component_v_select, {
            modelValue: config.value.scan_scope,
            "onUpdate:modelValue": _cache[2] || (_cache[2] = $event => ((config.value.scan_scope) = $event)),
            items: [{title:'仅媒体库',value:'media_only'},{title:'仅源数据',value:'source_only'},{title:'全部',value:'all'}],
            label: "扫描范围",
            variant: "outlined",
            density: "compact"
          }, null, 8, ["modelValue"]),
          _createVNode(_component_v_switch, {
            modelValue: config.value.allow_delete,
            "onUpdate:modelValue": _cache[3] || (_cache[3] = $event => ((config.value.allow_delete) = $event)),
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
            onClick: _cache[4] || (_cache[4] = $event => (emit('save', config.value)))
          }, {
            default: _withCtx(() => [...(_cache[6] || (_cache[6] = [
              _createTextVNode("保存", -1)
            ]))]),
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
