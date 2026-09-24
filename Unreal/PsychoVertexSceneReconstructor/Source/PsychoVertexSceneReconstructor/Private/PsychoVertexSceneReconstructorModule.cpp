#include "PsychoVertexSceneReconstructorModule.h"

#include "PVMSceneReconstructorWidget.h"
#include "Framework/Docking/TabManager.h"
#include "ToolMenus.h"
#include "Widgets/Docking/SDockTab.h"

#define LOCTEXT_NAMESPACE "PsychoVertexSceneReconstructorModule"

static const FName PVMTabName(TEXT("PsychoVertexSceneReconstructor"));

void FPsychoVertexSceneReconstructorModule::StartupModule()
{
    FGlobalTabmanager::Get()->RegisterNomadTabSpawner(PVMTabName,
        FOnSpawnTab::CreateLambda([](const FSpawnTabArgs&)
        {
            return SNew(SDockTab).TabRole(ETabRole::NomadTab)[SNew(SPVMSceneReconstructorWidget)];
        }))
        .SetDisplayName(LOCTEXT("TabTitle", "PsychoVertex Scene Reconstructor"))
        .SetMenuType(ETabSpawnerMenuType::Hidden);
    UToolMenus::RegisterStartupCallback(FSimpleMulticastDelegate::FDelegate::CreateRaw(this, &FPsychoVertexSceneReconstructorModule::RegisterMenus));
}

void FPsychoVertexSceneReconstructorModule::ShutdownModule()
{
    UToolMenus::UnRegisterStartupCallback(this);
    UToolMenus::UnregisterOwner(this);
    FGlobalTabmanager::Get()->UnregisterNomadTabSpawner(PVMTabName);
}

void FPsychoVertexSceneReconstructorModule::RegisterMenus()
{
    FToolMenuOwnerScoped Owner(this);
    UToolMenu* Menu = UToolMenus::Get()->ExtendMenu(TEXT("LevelEditor.MainMenu.Window"));
    FToolMenuSection& Section = Menu->FindOrAddSection(TEXT("WindowLayout"));
    Section.AddMenuEntry(
        TEXT("OpenPsychoVertexSceneReconstructor"),
        LOCTEXT("OpenLabel", "PsychoVertex Scene Reconstructor"),
        LOCTEXT("OpenTooltip", "Validate and reconstruct a PVM baked scene in the current level."),
        FSlateIcon(),
        FUIAction(FExecuteAction::CreateLambda([] { FGlobalTabmanager::Get()->TryInvokeTab(PVMTabName); })));
}

IMPLEMENT_MODULE(FPsychoVertexSceneReconstructorModule, PsychoVertexSceneReconstructor)

#undef LOCTEXT_NAMESPACE
