import { importShared } from './__federation_fn_import.js';
import _sfc_main$1 from './__federation_expose_AppPage.js';

const {createVNode:_createVNode,resolveComponent:_resolveComponent,withCtx:_withCtx,openBlock:_openBlock,createBlock:_createBlock} = await importShared('vue');

const _sfc_main = {
  __name: 'Page',
  props: {
  api: { type: Object, required: true },
  pluginId: { type: String, required: true },
},
  setup(__props) {



return (_ctx, _cache) => {
  const _component_v_card_text = _resolveComponent("v-card-text");
  const _component_v_card = _resolveComponent("v-card");

  return (_openBlock(), _createBlock(_component_v_card, { variant: "flat" }, {
    default: _withCtx(() => [
      _createVNode(_component_v_card_text, null, {
        default: _withCtx(() => [
          _createVNode(_sfc_main$1, {
            api: __props.api,
            pluginId: __props.pluginId
          }, null, 8, ["api", "pluginId"])
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
