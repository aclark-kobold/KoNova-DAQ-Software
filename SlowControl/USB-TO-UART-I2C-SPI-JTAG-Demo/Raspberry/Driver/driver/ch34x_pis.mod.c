#include <linux/module.h>
#define INCLUDE_VERMAGIC
#include <linux/build-salt.h>
#include <linux/elfnote-lto.h>
#include <linux/vermagic.h>
#include <linux/compiler.h>

BUILD_SALT;
BUILD_LTO_INFO;

MODULE_INFO(vermagic, VERMAGIC_STRING);
MODULE_INFO(name, KBUILD_MODNAME);

__visible struct module __this_module
__section(".gnu.linkonce.this_module") = {
	.name = KBUILD_MODNAME,
	.init = init_module,
#ifdef CONFIG_MODULE_UNLOAD
	.exit = cleanup_module,
#endif
	.arch = MODULE_ARCH_INIT,
};

#ifdef CONFIG_RETPOLINE
MODULE_INFO(retpoline, "Y");
#endif

static const struct modversion_info ____versions[]
__used __section("__versions") = {
	{ 0x3d49336c, "module_layout" },
	{ 0x5ef3c929, "usb_deregister" },
	{ 0x4edf89e, "usb_register_driver" },
	{ 0xc3aaf0a9, "__put_user_1" },
	{ 0x34f3402e, "usb_control_msg" },
	{ 0xb2fd5ceb, "__put_user_4" },
	{ 0x6729d3df, "__get_user_4" },
	{ 0x167e7f9d, "__get_user_1" },
	{ 0xba8fbd64, "_raw_spin_lock" },
	{ 0x92997ed8, "_printk" },
	{ 0x13884ad7, "usb_deregister_dev" },
	{ 0x108edc61, "usb_autopm_put_interface" },
	{ 0x53e2a0d3, "usb_put_dev" },
	{ 0x70fc49b1, "_dev_info" },
	{ 0x9645dc3f, "usb_register_dev" },
	{ 0xcefb0c9f, "__mutex_init" },
	{ 0xc87afba8, "usb_get_dev" },
	{ 0xd9a5ea54, "__init_waitqueue_head" },
	{ 0x962c8ae1, "usb_kill_anchored_urbs" },
	{ 0x407af304, "usb_wait_anchor_empty_timeout" },
	{ 0xd0da656b, "__stack_chk_fail" },
	{ 0x6b10bee1, "_copy_to_user" },
	{ 0x6dbe35be, "usb_bulk_msg" },
	{ 0xeb233a45, "__kmalloc" },
	{ 0x296695f, "refcount_warn_saturate" },
	{ 0xee360646, "usb_find_interface" },
	{ 0x3494e2cd, "usb_unanchor_urb" },
	{ 0x22921843, "_dev_err" },
	{ 0x4934b7f2, "usb_submit_urb" },
	{ 0x18c24f44, "usb_anchor_urb" },
	{ 0x4dfa8d4b, "mutex_lock" },
	{ 0x69acdf38, "memcpy" },
	{ 0xafe37b13, "usb_alloc_coherent" },
	{ 0x88db9f48, "__check_object_size" },
	{ 0xcf2a6966, "up" },
	{ 0xa5bbf969, "usb_free_urb" },
	{ 0xb5817cf1, "usb_free_coherent" },
	{ 0x37a0cba, "kfree" },
	{ 0x13c49cc2, "_copy_from_user" },
	{ 0xbfd2ca5c, "kmem_cache_alloc_trace" },
	{ 0x236b06ba, "kmalloc_caches" },
	{ 0x8bbcd78a, "usb_alloc_urb" },
	{ 0x98604f1c, "pv_ops" },
	{ 0x8427cc7b, "_raw_spin_lock_irq" },
	{ 0x6bd0e573, "down_interruptible" },
	{ 0x3213f038, "mutex_unlock" },
	{ 0x5b8239ca, "__x86_return_thunk" },
	{ 0xbdfb6dbb, "__fentry__" },
};

MODULE_INFO(depends, "");

MODULE_ALIAS("usb:v1A86p5512d*dc*dsc*dp*ic*isc*ip*in*");
MODULE_ALIAS("usb:v1A86p55DBd*dc*dsc*dp*ic*isc*ip*in02*");
MODULE_ALIAS("usb:v1A86p55DDd*dc*dsc*dp*ic*isc*ip*in02*");

MODULE_INFO(srcversion, "693D35E4BF13261A2347EF2");
