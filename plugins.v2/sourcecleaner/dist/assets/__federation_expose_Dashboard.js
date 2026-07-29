import { importShared } from './__federation_fn_import.js';
import { _ as _export_sfc } from './_plugin-vue_export-helper.js';

const _sfc_main = {
  name: 'Dashboard',
  props: {
    config: { type: Object, default: () => ({}) },
    allowRefresh: { type: Boolean, default: false },
  },
  data: () => ({ summary: null }),
  mounted() { this.fetch(); },
  methods: {
    async fetch() {
      try {
        const resp = await this.$root.$api?.get('plugin/LibraryCleaner/result');
        if (resp?.data) this.summary = resp.data;
      } catch (e) { /* ignore */ }
    },
    formatSize(n) {
      if (!n) return '0 B';
      const u = ['B','KB','MB','GB','TB'];
      let i = 0, s = n;
      while (Math.abs(s) >= 1024 && i < u.length-1) { s /= 1024; i++; }
      return s.toFixed(1) + ' ' + u[i];
    },
  },
};

const {createTextVNode:_createTextVNode,resolveComponent:_resolveComponent,withCtx:_withCtx,createVNode:_createVNode,toDisplayString:_toDisplayString,openBlock:_openBlock,createElementBlock:_createElementBlock,createCommentVNode:_createCommentVNode,createBlock:_createBlock} = await importShared('vue');


const _hoisted_1 = {
  key: 0,
  class: "text-h5 font-weight-bold text-primary"
};
const _hoisted_2 = {
  key: 1,
  class: "text-caption text-medium-emphasis"
};
const _hoisted_3 = {
  key: 2,
  class: "text-caption text-grey"
};

function _sfc_render(_ctx, _cache, $props, $setup, $data, $options) {
  const _component_v_card_title = _resolveComponent("v-card-title");
  const _component_v_card_text = _resolveComponent("v-card-text");
  const _component_v_card = _resolveComponent("v-card");

  return (_openBlock(), _createBlock(_component_v_card, { variant: "outlined" }, {
    default: _withCtx(() => [
      _createVNode(_component_v_card_title, { class: "text-subtitle-1" }, {
        default: _withCtx(() => [...(_cache[0] || (_cache[0] = [
          _createTextVNode("源文件清理", -1)
        ]))]),
        _: 1
      }),
      _createVNode(_component_v_card_text, { class: "text-center py-4" }, {
        default: _withCtx(() => [
          (_ctx.summary)
            ? (_openBlock(), _createElementBlock("div", _hoisted_1, _toDisplayString(_ctx.summary.total), 1))
            : _createCommentVNode("", true),
          (_ctx.summary)
            ? (_openBlock(), _createElementBlock("div", _hoisted_2, "项残留 · " + _toDisplayString($options.formatSize(_ctx.summary.total_size)), 1))
            : (_openBlock(), _createElementBlock("div", _hoisted_3, "暂无数据"))
        ]),
        _: 1
      })
    ]),
    _: 1
  }))
}
const Dashboard = /*#__PURE__*/_export_sfc(_sfc_main, [['render',_sfc_render]]);

export { Dashboard as default };
