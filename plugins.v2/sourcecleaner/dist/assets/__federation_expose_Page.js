import { importShared } from './__federation_fn_import.js';
import AppPage from './__federation_expose_AppPage.js';
import { _ as _export_sfc } from './_plugin-vue_export-helper.js';

const _sfc_main = {
  name: 'Page',
  components: { AppPage },
  props: {
    api: { type: Object, required: true },
    pluginId: { type: String, required: true },
  },
};

const {resolveComponent:_resolveComponent,createVNode:_createVNode,withCtx:_withCtx,openBlock:_openBlock,createBlock:_createBlock} = await importShared('vue');


function _sfc_render(_ctx, _cache, $props, $setup, $data, $options) {
  const _component_AppPage = _resolveComponent("AppPage");
  const _component_v_card_text = _resolveComponent("v-card-text");
  const _component_v_card = _resolveComponent("v-card");

  return (_openBlock(), _createBlock(_component_v_card, { variant: "flat" }, {
    default: _withCtx(() => [
      _createVNode(_component_v_card_text, null, {
        default: _withCtx(() => [
          _createVNode(_component_AppPage, {
            api: $props.api,
            pluginId: $props.pluginId
          }, null, 8, ["api", "pluginId"])
        ]),
        _: 1
      })
    ]),
    _: 1
  }))
}
const Page = /*#__PURE__*/_export_sfc(_sfc_main, [['render',_sfc_render]]);

export { Page as default };
