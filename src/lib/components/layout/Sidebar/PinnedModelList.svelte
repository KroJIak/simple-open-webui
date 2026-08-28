<script>
	import Sortable from 'sortablejs';

	import { onMount, tick } from 'svelte';

	import { chatId, config, mobile, models, settings, showSidebar } from '$lib/stores';
	import { WEBUI_BASE_URL } from '$lib/constants';
	import { updateUserSettings } from '$lib/apis/users';
	import PinnedModelItem from './PinnedModelItem.svelte';

	export let selectedChatId = null;
	export let shiftKey = false;

	let pinnedModels = [];

	const isVisibleModel = (id) => {
		const model = $models.find((m) => m.id === id);
		return model && !(model?.info?.meta?.hidden ?? model?.meta?.hidden ?? false);
	};

	const getDefaultPinnedModels = () =>
		($config?.default_pinned_models ?? '').split(',').filter((id) => id).filter(isVisibleModel);

	const getEffectivePinnedModels = () => {
		if ($settings?.pinnedModelsCustomized === true) {
			return ($settings?.pinnedModels ?? []).filter(isVisibleModel);
		}

		return getDefaultPinnedModels();
	};

	const initPinnedModelsSortable = () => {
		const pinnedModelsList = document.getElementById('pinned-models-list');
		if (pinnedModelsList && !$mobile) {
			new Sortable(pinnedModelsList, {
				animation: 150,
				onUpdate: async (event) => {
					const modelId = event.item.dataset.id;
					const newIndex = event.newIndex;

					const pinnedModels = [...getEffectivePinnedModels()];
					const oldIndex = pinnedModels.indexOf(modelId);

					pinnedModels.splice(oldIndex, 1);
					pinnedModels.splice(newIndex, 0, modelId);

					settings.set({
						...$settings,
						pinnedModels: pinnedModels,
						pinnedModelsCustomized: true
					});
					await updateUserSettings(localStorage.token, { ui: $settings });
				}
			});
		}
	};

	const cleanupStalePinnedModels = async (modelIds) => {
		const validModels = modelIds.filter(isVisibleModel);

		if (validModels.length !== modelIds.length) {
			pinnedModels = validModels;
			if ($settings?.pinnedModelsCustomized === true) {
				settings.set({
					...$settings,
					pinnedModels: validModels,
					pinnedModelsCustomized: true
				});
				await updateUserSettings(localStorage.token, { ui: $settings });
			}
		}
	};

	$: pinnedModels = getEffectivePinnedModels();

	onMount(async () => {
		if (pinnedModels.length > 0) {
			await cleanupStalePinnedModels(pinnedModels);
		}

		await tick();
		initPinnedModelsSortable();
	});
</script>

<div class="mt-0.5 pb-1.5" id="pinned-models-list">
	{#each pinnedModels as modelId (modelId)}
		{@const model = $models.find((model) => model.id === modelId)}
		{#if model}
			<PinnedModelItem
				{model}
				{shiftKey}
				onClick={() => {
					selectedChatId = null;
					chatId.set('');
					if ($mobile) {
						showSidebar.set(false);
					}
				}}
				onUnpin={() => {
					const nextPinnedModels = getEffectivePinnedModels().filter((id) => id !== modelId);
					settings.set({
						...$settings,
						pinnedModels: nextPinnedModels,
						pinnedModelsCustomized: true
					});
					updateUserSettings(localStorage.token, { ui: $settings });
				}}
			/>
		{/if}
	{/each}
</div>
