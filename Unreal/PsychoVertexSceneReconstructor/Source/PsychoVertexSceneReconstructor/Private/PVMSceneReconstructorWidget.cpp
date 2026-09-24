#include "PVMSceneReconstructorWidget.h"

#include "AssetRegistry/AssetRegistryModule.h"
#include "AssetRegistry/IAssetRegistry.h"
#include "ContentBrowserModule.h"
#include "IContentBrowserSingleton.h"
#include "DesktopPlatformModule.h"
#include "Editor.h"
#include "Subsystems/EditorActorSubsystem.h"
#include "Engine/StaticMesh.h"
#include "Engine/StaticMeshActor.h"
#include "Framework/Application/SlateApplication.h"
#include "HAL/FileManager.h"
#include "IDesktopPlatform.h"
#include "JsonObjectConverter.h"
#include "Misc/FileHelper.h"
#include "Misc/MessageDialog.h"
#include "Misc/Paths.h"
#include "ScopedTransaction.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "Widgets/Input/SButton.h"
#include "Widgets/Input/SEditableTextBox.h"
#include "Widgets/Input/SMultiLineEditableTextBox.h"
#include "Widgets/Layout/SBorder.h"
#include "Widgets/Layout/SBox.h"
#include "Widgets/Layout/SScrollBox.h"
#include "Widgets/Layout/SUniformGridPanel.h"
#include "Widgets/Text/STextBlock.h"

#define LOCTEXT_NAMESPACE "PVMSceneReconstructor"

namespace
{
bool ReadNumberArray(const TSharedPtr<FJsonObject>& Object, const TCHAR* Field, int32 Count, TArray<double>& Out)
{
    const TArray<TSharedPtr<FJsonValue>>* Values = nullptr;
    if (!Object.IsValid() || !Object->TryGetArrayField(Field, Values) || Values->Num() != Count)
    {
        return false;
    }
    Out.Reset(Count);
    for (const TSharedPtr<FJsonValue>& Value : *Values)
    {
        double Number = 0.0;
        if (!Value.IsValid() || !Value->TryGetNumber(Number) || !FMath::IsFinite(Number))
        {
            return false;
        }
        Out.Add(Number);
    }
    return true;
}

FString ActorTag(const TCHAR* Prefix, const FString& Value)
{
    return FString(Prefix) + Value;
}
}

void SPVMSceneReconstructorWidget::Construct(const FArguments& InArgs)
{
    ChildSlot
    [
        SNew(SBorder).Padding(12)
        [
            SNew(SScrollBox)
            + SScrollBox::Slot()
            [
                SNew(SVerticalBox)
                + SVerticalBox::Slot().AutoHeight().Padding(0, 0, 0, 10)
                [SNew(STextBlock).Text(LOCTEXT("Instructions", "Import and configure the exported FBXs first, then open the target level. This tool never imports assets or changes materials."))
                    .AutoWrapText(true)]
                + SVerticalBox::Slot().AutoHeight().Padding(0, 3)
                [SNew(STextBlock).Text(LOCTEXT("FolderLabel", "Static Mesh Content Folder"))]
                + SVerticalBox::Slot().AutoHeight()
                [
                    SNew(SHorizontalBox)
                    + SHorizontalBox::Slot().FillWidth(1)
                    [SAssignNew(AssetFolderText, SEditableTextBox).Text(FText::FromString(TEXT("/Game")))
                        .OnTextChanged(this, &SPVMSceneReconstructorWidget::InvalidateValidation)]
                    + SHorizontalBox::Slot().AutoWidth().Padding(6, 0, 0, 0)
                    [SNew(SButton).Text(LOCTEXT("UseSelected", "Use Selected Folder"))
                        .OnClicked(this, &SPVMSceneReconstructorWidget::UseSelectedContentFolder)]
                ]
                + SVerticalBox::Slot().AutoHeight().Padding(0, 10, 0, 3)
                [SNew(STextBlock).Text(LOCTEXT("JsonLabel", "PVMScene.json"))]
                + SVerticalBox::Slot().AutoHeight()
                [
                    SNew(SHorizontalBox)
                    + SHorizontalBox::Slot().FillWidth(1)
                    [SAssignNew(JsonPathText, SEditableTextBox).OnTextChanged(this, &SPVMSceneReconstructorWidget::InvalidateValidation)]
                    + SHorizontalBox::Slot().AutoWidth().Padding(6, 0, 0, 0)
                    [SNew(SButton).Text(LOCTEXT("Browse", "Browse..."))
                        .OnClicked(this, &SPVMSceneReconstructorWidget::BrowseJson)]
                ]
                + SVerticalBox::Slot().AutoHeight().Padding(0, 12)
                [
                    SNew(SUniformGridPanel).SlotPadding(FMargin(4, 0))
                    + SUniformGridPanel::Slot(0, 0)
                    [SNew(SButton).Text(LOCTEXT("Validate", "Validate")).OnClicked(this, &SPVMSceneReconstructorWidget::Validate)]
                    + SUniformGridPanel::Slot(1, 0)
                    [SNew(SButton).Text(LOCTEXT("Reconstruct", "Reconstruct Current Level")).OnClicked(this, &SPVMSceneReconstructorWidget::Reconstruct)]
                    + SUniformGridPanel::Slot(2, 0)
                    [SNew(SButton).Text(LOCTEXT("DeletePVMFolder", "Delete PVM Folder Actors"))
                        .ToolTipText(LOCTEXT("DeletePVMFolderTooltip", "Delete every actor under the PVM Outliner folder. This operation can be undone."))
                        .OnClicked(this, &SPVMSceneReconstructorWidget::DeletePVMFolderActors)]
                ]
                + SVerticalBox::Slot().AutoHeight().Padding(0, 6)
                [SAssignNew(StatusText, SMultiLineEditableTextBox).IsReadOnly(true).Text(LOCTEXT("InitialStatus", "Not validated."))]
            ]
        ]
    ];
}

void SPVMSceneReconstructorWidget::InvalidateValidation(const FText&)
{
    bValidationSucceeded = false;
    Validated.Reset();
    SetStatus(TEXT("Inputs changed. Validate again before reconstruction."));
}

void SPVMSceneReconstructorWidget::SetStatus(const FString& Message)
{
    if (StatusText.IsValid())
    {
        StatusText->SetText(FText::FromString(Message));
    }
}

FReply SPVMSceneReconstructorWidget::BrowseJson()
{
    IDesktopPlatform* Desktop = FDesktopPlatformModule::Get();
    if (!Desktop)
    {
        SetStatus(TEXT("Desktop file dialog is unavailable."));
        return FReply::Handled();
    }
    TArray<FString> Files;
    const void* Parent = FSlateApplication::Get().FindBestParentWindowHandleForDialogs(nullptr);
    if (Desktop->OpenFileDialog(Parent, TEXT("Select PVM Scene JSON"), TEXT(""), TEXT("PVMScene.json"),
        TEXT("JSON files (*.json)|*.json"), EFileDialogFlags::None, Files) && Files.Num() == 1)
    {
        JsonPathText->SetText(FText::FromString(Files[0]));
    }
    return FReply::Handled();
}

FReply SPVMSceneReconstructorWidget::UseSelectedContentFolder()
{
    FContentBrowserModule& Browser = FModuleManager::LoadModuleChecked<FContentBrowserModule>(TEXT("ContentBrowser"));
    TArray<FString> Folders;
    Browser.Get().GetSelectedFolders(Folders);
    if (Folders.Num() != 1)
    {
        SetStatus(TEXT("Select exactly one folder in the Content Browser, then try again."));
    }
    else
    {
        AssetFolderText->SetText(FText::FromString(Folders[0]));
    }
    return FReply::Handled();
}

FReply SPVMSceneReconstructorWidget::Validate()
{
    FString Summary;
    bValidationSucceeded = ValidateInputs(Summary);
    SetStatus(Summary);
    return FReply::Handled();
}

bool SPVMSceneReconstructorWidget::ValidateInputs(FString& OutSummary)
{
    Validated.Reset();
    TArray<FString> Errors;
    const FString JsonPath = JsonPathText->GetText().ToString();
    const FString AssetFolder = AssetFolderText->GetText().ToString();
    if (!AssetFolder.StartsWith(TEXT("/Game")))
    {
        Errors.Add(TEXT("Content folder must begin with /Game."));
    }
    FString JsonText;
    if (!FFileHelper::LoadFileToString(JsonText, *JsonPath))
    {
        Errors.Add(FString::Printf(TEXT("Could not read JSON: %s"), *JsonPath));
    }
    TSharedPtr<FJsonObject> Root;
    const TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(JsonText);
    if (Errors.IsEmpty() && (!FJsonSerializer::Deserialize(Reader, Root) || !Root.IsValid()))
    {
        Errors.Add(TEXT("Malformed JSON document."));
    }
    if (Root.IsValid())
    {
        FString Schema;
        double Version = 0;
        if (!Root->TryGetStringField(TEXT("schema"), Schema) || Schema != TEXT("pvm.unreal_scene") ||
            !Root->TryGetNumberField(TEXT("version"), Version) || Version != 1.0)
        {
            Errors.Add(TEXT("Unsupported schema or version; expected pvm.unreal_scene version 1."));
        }
    }

    TMap<FString, FString> MeshNameByAsset;
    TSet<FString> SeenMeshNames;
    const TArray<TSharedPtr<FJsonValue>>* Assets = nullptr;
    if (Root.IsValid() && Root->TryGetArrayField(TEXT("assets"), Assets))
    {
        for (const TSharedPtr<FJsonValue>& Value : *Assets)
        {
            const TSharedPtr<FJsonObject> Item = Value->AsObject();
            FString Id, MeshName;
            if (!Item.IsValid() || !Item->TryGetStringField(TEXT("id"), Id) || Id.IsEmpty() ||
                !Item->TryGetStringField(TEXT("static_mesh_name"), MeshName) || MeshName.IsEmpty())
            {
                Errors.Add(TEXT("Asset entry has a missing id or static_mesh_name."));
                continue;
            }
            if (MeshNameByAsset.Contains(Id) || SeenMeshNames.Contains(MeshName))
            {
                Errors.Add(FString::Printf(TEXT("Duplicate asset id or Static Mesh name: %s / %s"), *Id, *MeshName));
                continue;
            }
            MeshNameByAsset.Add(Id, MeshName);
            SeenMeshNames.Add(MeshName);
        }
    }
    else if (Root.IsValid())
    {
        Errors.Add(TEXT("Missing assets array."));
    }

    if (Errors.IsEmpty())
    {
        FARFilter Filter;
        Filter.PackagePaths.Add(*AssetFolder);
        Filter.bRecursivePaths = true;
        Filter.ClassPaths.Add(UStaticMesh::StaticClass()->GetClassPathName());
        TArray<FAssetData> Found;
        FModuleManager::LoadModuleChecked<FAssetRegistryModule>(TEXT("AssetRegistry")).Get().GetAssets(Filter, Found);
        TMap<FString, TArray<FAssetData>> ByName;
        for (const FAssetData& Data : Found)
        {
            ByName.FindOrAdd(Data.AssetName.ToString()).Add(Data);
        }
        for (const TPair<FString, FString>& Pair : MeshNameByAsset)
        {
            const TArray<FAssetData>* Matches = ByName.Find(Pair.Value);
            if (!Matches || Matches->Num() == 0)
            {
                Errors.Add(FString::Printf(TEXT("Missing Static Mesh: %s"), *Pair.Value));
            }
            else if (Matches->Num() != 1)
            {
                Errors.Add(FString::Printf(TEXT("Ambiguous Static Mesh name '%s' (%d matches)."), *Pair.Value, Matches->Num()));
            }
            else if (UStaticMesh* Mesh = Cast<UStaticMesh>((*Matches)[0].GetAsset()))
            {
                Validated.MeshesByAssetId.Add(Pair.Key, Mesh);
                Validated.MeshNamesByAssetId.Add(Pair.Key, Pair.Value);
            }
            else
            {
                Errors.Add(FString::Printf(TEXT("Could not load Static Mesh: %s"), *Pair.Value));
            }
        }
    }

    const TSharedPtr<FJsonObject>* Scene = nullptr;
    if (Root.IsValid() && Root->TryGetObjectField(TEXT("scene"), Scene) && Scene && Scene->IsValid())
    {
        if (!(*Scene)->TryGetStringField(TEXT("reconstruction_id"), Validated.ReconstructionId) || Validated.ReconstructionId.IsEmpty())
        {
            Errors.Add(TEXT("Missing scene.reconstruction_id."));
        }
    }
    else if (Root.IsValid())
    {
        Errors.Add(TEXT("Missing scene object."));
    }

    TSet<FString> PlacementIds;
    const TArray<TSharedPtr<FJsonValue>>* Placements = nullptr;
    if (Root.IsValid() && Root->TryGetArrayField(TEXT("placements"), Placements))
    {
        for (const TSharedPtr<FJsonValue>& Value : *Placements)
        {
            const TSharedPtr<FJsonObject> Item = Value->AsObject();
            FPVMPlacement Placement;
            double Batch = 0;
            const TSharedPtr<FJsonObject>* Transform = nullptr;
            if (!Item.IsValid() || !Item->TryGetStringField(TEXT("id"), Placement.Id) || Placement.Id.IsEmpty() ||
                PlacementIds.Contains(Placement.Id) || !Item->TryGetStringField(TEXT("asset_id"), Placement.AssetId) ||
                !Item->TryGetStringField(TEXT("kind"), Placement.Kind) || !Item->TryGetNumberField(TEXT("source_batch"), Batch) ||
                !Item->TryGetObjectField(TEXT("transform"), Transform) || !Transform || !Transform->IsValid())
            {
                Errors.Add(TEXT("Malformed or duplicate placement entry."));
                continue;
            }
            PlacementIds.Add(Placement.Id);
            Placement.SourceBatch = static_cast<int32>(Batch);
            if (!MeshNameByAsset.Contains(Placement.AssetId))
            {
                Errors.Add(FString::Printf(TEXT("Placement '%s' references unknown asset '%s'."), *Placement.Id, *Placement.AssetId));
                continue;
            }
            TArray<double> Location, Rotation, Scale;
            if (!ReadNumberArray(*Transform, TEXT("location_cm"), 3, Location) ||
                !ReadNumberArray(*Transform, TEXT("rotation_xyzw"), 4, Rotation) ||
                !ReadNumberArray(*Transform, TEXT("scale"), 3, Scale) ||
                FMath::IsNearlyZero(Scale[0]) || FMath::IsNearlyZero(Scale[1]) || FMath::IsNearlyZero(Scale[2]))
            {
                Errors.Add(FString::Printf(TEXT("Placement '%s' has an invalid transform."), *Placement.Id));
                continue;
            }
            FQuat Quaternion(Rotation[0], Rotation[1], Rotation[2], Rotation[3]);
            if (!Quaternion.IsNormalized())
            {
                if (Quaternion.SizeSquared() <= UE_SMALL_NUMBER)
                {
                    Errors.Add(FString::Printf(TEXT("Placement '%s' has a zero quaternion."), *Placement.Id));
                    continue;
                }
                Quaternion.Normalize();
            }
            Placement.Transform = FTransform(Quaternion, FVector(Location[0], Location[1], Location[2]), FVector(Scale[0], Scale[1], Scale[2]));
            Validated.Placements.Add(MoveTemp(Placement));
        }
    }
    else if (Root.IsValid())
    {
        Errors.Add(TEXT("Missing placements array."));
    }

    if (!Errors.IsEmpty())
    {
        Validated.Reset();
        OutSummary = FString::Printf(TEXT("Validation failed with %d error(s):\n- %s"), Errors.Num(), *FString::Join(Errors, TEXT("\n- ")));
        return false;
    }
    OutSummary = FString::Printf(TEXT("Validation succeeded.\n%d logical assets referenced\n%d Static Meshes resolved\n%d placements\n0 missing assets"),
        MeshNameByAsset.Num(), Validated.MeshesByAssetId.Num(), Validated.Placements.Num());
    return true;
}

FReply SPVMSceneReconstructorWidget::Reconstruct()
{
    if (!bValidationSucceeded)
    {
        SetStatus(TEXT("Validate the current inputs successfully before reconstruction."));
        return FReply::Handled();
    }
    UWorld* World = GEditor ? GEditor->GetEditorWorldContext().World() : nullptr;
    UEditorActorSubsystem* ActorSubsystem = GEditor ? GEditor->GetEditorSubsystem<UEditorActorSubsystem>() : nullptr;
    if (!World || !ActorSubsystem)
    {
        SetStatus(TEXT("No editable level is currently open."));
        return FReply::Handled();
    }
    const FString SceneTagText = ActorTag(TEXT("PVMScene="), Validated.ReconstructionId);
    TArray<AActor*> Existing;
    for (AActor* Actor : ActorSubsystem->GetAllLevelActors())
    {
        if (Actor && Actor->Tags.Contains(*SceneTagText))
        {
            Existing.Add(Actor);
        }
    }
    if (Existing.Num() > 0 && FMessageDialog::Open(EAppMsgType::YesNo,
        FText::Format(LOCTEXT("ReplacePrompt", "Replace {0} actors from the previous reconstruction?"), FText::AsNumber(Existing.Num()))) != EAppReturnType::Yes)
    {
        return FReply::Handled();
    }

    const FScopedTransaction Transaction(LOCTEXT("ReconstructTransaction", "Reconstruct PsychoVertex Scene"));
    World->Modify();
    int32 Spawned = 0;
    TArray<AStaticMeshActor*> Created;
    for (const FPVMPlacement& Placement : Validated.Placements)
    {
        UStaticMesh* const* Mesh = Validated.MeshesByAssetId.Find(Placement.AssetId);
        if (!Mesh || !*Mesh)
        {
            continue;
        }
        AStaticMeshActor* Actor = World->SpawnActor<AStaticMeshActor>(AStaticMeshActor::StaticClass(), Placement.Transform);
        if (!Actor)
        {
            for (AStaticMeshActor* CreatedActor : Created)
            {
                ActorSubsystem->DestroyActor(CreatedActor);
            }
            SetStatus(FString::Printf(TEXT("Failed to spawn placement '%s'; previous reconstruction was preserved."), *Placement.Id));
            return FReply::Handled();
        }
        Created.Add(Actor);
        Actor->SetFlags(RF_Transactional);
        Actor->Modify();
        Actor->GetStaticMeshComponent()->SetStaticMesh(*Mesh);
        const FString& MeshName = Validated.MeshNamesByAssetId.FindChecked(Placement.AssetId);
        Actor->SetActorLabel(FString::Printf(TEXT("%s_%s"), *MeshName, *Placement.Id.Left(8)));
        Actor->Tags.AddUnique(*SceneTagText);
        Actor->Tags.AddUnique(*ActorTag(TEXT("PVMPlacement="), Placement.Id));
        Actor->SetFolderPath(*FString::Printf(TEXT("PVM/Batch%d/%s"), Placement.SourceBatch, *MeshName));
        ++Spawned;
    }
    for (AActor* Actor : Existing)
    {
        ActorSubsystem->DestroyActor(Actor);
    }
    SetStatus(FString::Printf(TEXT("Reconstruction complete: %d actors created, %d previous actors removed."), Spawned, Existing.Num()));
    return FReply::Handled();
}

FReply SPVMSceneReconstructorWidget::DeletePVMFolderActors()
{
    UWorld* World = GEditor ? GEditor->GetEditorWorldContext().World() : nullptr;
    UEditorActorSubsystem* ActorSubsystem = GEditor ? GEditor->GetEditorSubsystem<UEditorActorSubsystem>() : nullptr;
    if (!World || !ActorSubsystem)
    {
        SetStatus(TEXT("No editable level is currently open."));
        return FReply::Handled();
    }

    TArray<AActor*> ActorsToDelete;
    for (AActor* Actor : ActorSubsystem->GetAllLevelActors())
    {
        if (!Actor)
        {
            continue;
        }
        const FString Folder = Actor->GetFolderPath().ToString();
        if (Folder == TEXT("PVM") || Folder.StartsWith(TEXT("PVM/")))
        {
            ActorsToDelete.Add(Actor);
        }
    }
    if (ActorsToDelete.IsEmpty())
    {
        SetStatus(TEXT("No actors were found under the PVM Outliner folder."));
        return FReply::Handled();
    }

    const FText Prompt = FText::Format(
        LOCTEXT("DeletePVMFolderPrompt", "Delete all {0} actors under the PVM Outliner folder?\n\nThis can be undone."),
        FText::AsNumber(ActorsToDelete.Num()));
    if (FMessageDialog::Open(EAppMsgType::YesNo, Prompt) != EAppReturnType::Yes)
    {
        return FReply::Handled();
    }

    const FScopedTransaction Transaction(LOCTEXT("DeletePVMFolderTransaction", "Delete PVM Folder Actors"));
    World->Modify();
    int32 Deleted = 0;
    for (AActor* Actor : ActorsToDelete)
    {
        Actor->Modify();
        if (ActorSubsystem->DestroyActor(Actor))
        {
            ++Deleted;
        }
    }
    SetStatus(FString::Printf(TEXT("Deleted %d actor(s) under the PVM Outliner folder."), Deleted));
    return FReply::Handled();
}

#undef LOCTEXT_NAMESPACE
