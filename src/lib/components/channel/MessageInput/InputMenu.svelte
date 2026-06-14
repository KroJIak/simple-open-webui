<script lang="ts">
	import { getContext } from 'svelte';

	import Dropdown from '$lib/components/common/Dropdown.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Clip from '$lib/components/icons/Clip.svelte';

	const i18n = getContext('i18n');

	export let screenCaptureHandler: Function;
	export let uploadFilesHandler: Function;

	export let onClose: Function = () => {};

	let show = false;
</script>

<Dropdown
	bind:show
	on:change={(e) => {
		if (e.detail === false) {
			onClose();
		}
	}}
>
	<Tooltip content={$i18n.t('More')}>
		<slot />
	</Tooltip>

	<div slot="content">
		<div
			class="w-[200px] rounded-2xl px-1 py-1 border border-gray-100 dark:border-gray-800 z-999 bg-white dark:bg-gray-850 dark:text-white shadow-lg transition"
		>
			<button
				class="select-none flex w-full gap-2 items-center px-3 py-1.5 text-sm cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800/50 rounded-xl"
				type="button"
				on:click={() => {
					uploadFilesHandler();
					show = false;
				}}
			>
				<Clip />
				<div class="line-clamp-1">{$i18n.t('Upload Files')}</div>
			</button>
		</div>
	</div>
</Dropdown>
